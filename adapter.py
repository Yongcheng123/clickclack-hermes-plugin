"""Hermes Platform Adapter for ClickClack.

The adapter intentionally has no plugin-specific third-party dependencies.
Hermes Agent v0.19.0 already ships ``httpx`` and ``websockets``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import uuid
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
import websockets
from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

PLUGIN_VERSION = "0.1.0"
DEFAULT_BASE_URL = "https://app.clickclack.chat"
MESSAGE_EVENT_TYPES = frozenset({"message.created", "thread.reply_created"})
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})
RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


class ClickClackError(RuntimeError):
    """A sanitized ClickClack request failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return default


def _as_string_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        values: Iterable[Any] = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = value
    else:
        values = (value,)
    return {str(item).strip() for item in values if str(item).strip()}


def _normalize_base_url(value: Any) -> str:
    base_url = str(value or DEFAULT_BASE_URL).strip().rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an absolute http:// or https:// URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "base_url must not contain credentials, query parameters, or a fragment"
        )
    return base_url


def _safe_identifier(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "default"


def _parse_chat_id(chat_id: str) -> tuple[str, str]:
    route_type, separator, route_id = str(chat_id or "").partition(":")
    if separator and route_type in {"channel", "dm"} and route_id:
        return route_type, route_id
    # Backward-compatible fallback for explicit home-channel delivery.
    if chat_id:
        return "channel", str(chat_id)
    raise ValueError("chat_id is empty")


def _mention_pattern(handle: str) -> re.Pattern[str]:
    escaped = re.escape(handle.lstrip("@"))
    return re.compile(rf"(?<![\w.-])@{escaped}(?![\w.-])", re.IGNORECASE)


def _strip_mention(text: str, handle: str) -> tuple[str, bool]:
    if not handle:
        return text.strip(), False
    pattern = _mention_pattern(handle)
    matched = pattern.search(text)
    if not matched:
        return text.strip(), False
    cleaned = pattern.sub("", text, count=1)
    cleaned = re.sub(r"^[\s,:，：;；-]+", "", cleaned)
    return cleaned.strip(), True


def _parse_timestamp(value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except ValueError:
        return datetime.now(UTC)


def _retry_after(headers: Mapping[str, str]) -> float | None:
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _sanitize_error_text(text: str, token: str) -> str:
    sanitized = str(text or "").replace(token, "[REDACTED]") if token else str(text or "")
    sanitized = re.sub(r"ccb_[A-Za-z0-9._~-]+", "ccb_[REDACTED]", sanitized)
    return sanitized[:500]


@dataclass(frozen=True)
class ClickClackSettings:
    token: str
    base_url: str
    workspace_id: str
    default_channel_id: str
    allowed_channel_ids: frozenset[str]
    allowed_user_ids: frozenset[str]
    require_mention: bool
    allow_dms: bool
    allow_all_users: bool
    thread_mode: str
    skip_history_on_first_start: bool
    request_timeout_seconds: float
    reconnect_max_seconds: float

    @classmethod
    def from_config(cls, config: Any) -> ClickClackSettings:
        extra = getattr(config, "extra", {}) or {}
        token = str(os.getenv("CLICKCLACK_BOT_TOKEN") or extra.get("token") or "").strip()
        base_url = _normalize_base_url(extra.get("base_url"))
        workspace_id = str(extra.get("workspace_id") or "").strip()
        default_channel_id = str(extra.get("default_channel_id") or "").strip()
        allowed_channels = _as_string_set(extra.get("allowed_channel_ids"))
        if default_channel_id:
            allowed_channels.add(default_channel_id)
        allowed_users = _as_string_set(extra.get("allowed_user_ids"))
        thread_mode = str(extra.get("thread_mode") or "always").strip().lower()
        if thread_mode not in {"always", "existing", "never"}:
            raise ValueError("thread_mode must be one of: always, existing, never")
        try:
            request_timeout = float(extra.get("request_timeout_seconds", 30))
            reconnect_max = float(extra.get("reconnect_max_seconds", 30))
        except (TypeError, ValueError) as exc:
            raise ValueError("timeout values must be numbers") from exc
        return cls(
            token=token,
            base_url=base_url,
            workspace_id=workspace_id,
            default_channel_id=default_channel_id,
            allowed_channel_ids=frozenset(allowed_channels),
            allowed_user_ids=frozenset(allowed_users),
            require_mention=_as_bool(extra.get("require_mention"), True),
            allow_dms=_as_bool(extra.get("allow_dms"), False),
            allow_all_users=_as_bool(extra.get("allow_all_users"), False),
            thread_mode=thread_mode,
            skip_history_on_first_start=_as_bool(
                extra.get("skip_history_on_first_start"), True
            ),
            request_timeout_seconds=max(5.0, request_timeout),
            reconnect_max_seconds=max(2.0, reconnect_max),
        )

    def missing_fields(self) -> list[str]:
        missing: list[str] = []
        if not self.token:
            missing.append("CLICKCLACK_BOT_TOKEN")
        if not self.workspace_id:
            missing.append("workspace_id")
        if not self.default_channel_id and not self.allowed_channel_ids and not self.allow_dms:
            missing.append("default_channel_id or allowed_channel_ids")
        return missing


class ClickClackClient:
    """Small async client for the ClickClack endpoints used by the adapter."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: float = 30.0,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = _normalize_base_url(base_url)
        self.token = token
        self._http = httpx.AsyncClient(
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": f"clickclack-hermes-plugin/{PLUGIN_VERSION}",
            },
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            transport=transport,
        )

    def url(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        suffix = "/" + path.lstrip("/")
        url = self.base_url + suffix
        if params:
            query = urlencode(
                {key: value for key, value in params.items() if value is not None},
                doseq=True,
            )
            if query:
                url += "?" + query
        return url

    def websocket_url(
        self, workspace_id: str, after_cursor: str | None = None
    ) -> str:
        parsed = urlsplit(self.url("/api/realtime/ws"))
        scheme = "wss" if parsed.scheme == "https" else "ws"
        query = {"workspace_id": workspace_id}
        if after_cursor:
            query["after_cursor"] = after_cursor
        return urlunsplit((scheme, parsed.netloc, parsed.path, urlencode(query), ""))

    async def close(self) -> None:
        await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._http.request(
                method,
                self.url(path, params),
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise ClickClackError(
                _sanitize_error_text(f"network error: {exc}", self.token),
                retryable=True,
            ) from exc

        if response.status_code < 200 or response.status_code >= 300:
            detail = _sanitize_error_text(response.text, self.token)
            raise ClickClackError(
                f"ClickClack HTTP {response.status_code}: {detail}",
                status_code=response.status_code,
                retryable=response.status_code in RETRYABLE_STATUS_CODES,
                retry_after=_retry_after(response.headers),
            )
        if response.status_code in {204, 205} or not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise ClickClackError(
                f"ClickClack returned invalid JSON for {path}",
                status_code=response.status_code,
                retryable=False,
            ) from exc
        if not isinstance(payload, dict):
            raise ClickClackError(
                f"ClickClack returned an unexpected response for {path}",
                status_code=response.status_code,
                retryable=False,
            )
        return payload

    async def me(self) -> dict[str, Any]:
        return (await self.request("GET", "/api/me")).get("user") or {}

    async def workspace(self, workspace_id: str) -> dict[str, Any]:
        return (
            await self.request("GET", f"/api/workspaces/{workspace_id}")
        ).get("workspace") or {}

    async def channels(self, workspace_id: str) -> list[dict[str, Any]]:
        value = (
            await self.request("GET", f"/api/workspaces/{workspace_id}/channels")
        ).get("channels")
        return value if isinstance(value, list) else []

    async def message(self, message_id: str) -> dict[str, Any]:
        return (
            await self.request("GET", f"/api/messages/{message_id}")
        ).get("message") or {}

    async def events(
        self,
        workspace_id: str,
        *,
        after_cursor: str | None = None,
        limit: int = 200,
        include_tail: bool = False,
    ) -> dict[str, Any]:
        return await self.request(
            "GET",
            "/api/realtime/events",
            params={
                "workspace_id": workspace_id,
                "after_cursor": after_cursor,
                "limit": limit,
                "include_tail": "true" if include_tail else None,
            },
        )

    async def create_channel_message(
        self, channel_id: str, body: str, nonce: str
    ) -> dict[str, Any]:
        return (
            await self.request(
                "POST",
                f"/api/channels/{channel_id}/messages",
                json_body={"body": body, "nonce": nonce},
            )
        ).get("message") or {}

    async def create_direct_message(
        self, conversation_id: str, body: str, nonce: str
    ) -> dict[str, Any]:
        return (
            await self.request(
                "POST",
                f"/api/dms/{conversation_id}/messages",
                json_body={"body": body, "nonce": nonce},
            )
        ).get("message") or {}

    async def create_thread_reply(
        self, root_message_id: str, body: str, nonce: str
    ) -> dict[str, Any]:
        return (
            await self.request(
                "POST",
                f"/api/messages/{root_message_id}/thread/replies",
                json_body={"body": body, "nonce": nonce},
            )
        ).get("message") or {}

    async def edit_message(self, message_id: str, body: str) -> dict[str, Any]:
        return (
            await self.request(
                "PATCH",
                f"/api/messages/{message_id}",
                json_body={"body": body},
            )
        ).get("message") or {}

    async def publish_typing(
        self,
        workspace_id: str,
        *,
        channel_id: str | None = None,
        direct_conversation_id: str | None = None,
    ) -> None:
        await self.request(
            "POST",
            "/api/realtime/ephemeral",
            json_body={
                "workspace_id": workspace_id,
                "channel_id": channel_id,
                "direct_conversation_id": direct_conversation_id,
                "type": "typing.started",
                "payload": {},
            },
        )


class CursorStore:
    """Atomic, profile-aware durable realtime cursor storage."""

    def __init__(self, workspace_id: str, token: str) -> None:
        try:
            from hermes_constants import get_hermes_home

            hermes_home = Path(get_hermes_home())
        except (ImportError, TypeError):
            hermes_home = Path(
                os.getenv("HERMES_HOME") or (Path.home() / ".hermes")
            )
        token_fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
        filename = (
            f"{_safe_identifier(workspace_id)}-{token_fingerprint}.cursor.json"
        )
        self.path = hermes_home / "clickclack" / filename

    def load(self) -> str | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        cursor = payload.get("cursor") if isinstance(payload, dict) else None
        return str(cursor).strip() if cursor else None

    def save(self, cursor: str) -> None:
        if not cursor:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            self.path.parent.chmod(0o700)
        temporary = self.path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps({"cursor": cursor}, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with suppress(OSError):
            temporary.chmod(0o600)
        os.replace(temporary, self.path)


class ClickClackAdapter(BasePlatformAdapter):
    """ClickClack gateway adapter for Hermes Agent."""

    supports_code_blocks = True
    supports_async_delivery = True

    def __init__(self, config: Any, **_: Any) -> None:
        super().__init__(config=config, platform=Platform("clickclack"))
        self.settings = ClickClackSettings.from_config(config)
        self._client: ClickClackClient | None = None
        self._cursor_store = CursorStore(
            self.settings.workspace_id or "unconfigured",
            self.settings.token or "unconfigured",
        )
        self._cursor: str | None = None
        self._ws: Any = None
        self._receive_task: asyncio.Task[Any] | None = None
        self._closing = False
        self._bot: dict[str, Any] = {}
        self._bot_user_id = ""
        self._bot_handle = ""
        self._effective_allowed_user_ids: set[str] = set(
            self.settings.allowed_user_ids
        )
        self._channel_names: dict[str, str] = {}
        self._lock_key: str | None = None

        # Hermes' config-only authorization bridge trusts an adapter only when
        # these effective policies are allowlists and intake actually enforces
        # them. This adapter does both.
        self._dm_policy = "allowlist"
        self._group_policy = "allowlist"

    @property
    def name(self) -> str:
        return "ClickClack"

    @property
    def enforces_own_access_policy(self) -> bool:
        return True

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        missing = self.settings.missing_fields()
        if missing:
            detail = "Missing ClickClack configuration: " + ", ".join(missing)
            logger.error(detail)
            self._set_fatal_error("config_missing", detail, retryable=False)
            return False

        self._closing = False
        if not self._acquire_token_lock():
            return False

        try:
            self._client = ClickClackClient(
                self.settings.base_url,
                self.settings.token,
                self.settings.request_timeout_seconds,
            )
            self._bot = await self._client.me()
            self._bot_user_id = str(self._bot.get("id") or "")
            self._bot_handle = str(self._bot.get("handle") or "").lstrip("@")
            if (
                not self._bot_user_id
                or not self._bot_handle
                or self._bot.get("kind") != "bot"
            ):
                raise ClickClackError(
                    "CLICKCLACK_BOT_TOKEN did not resolve to a valid bot identity"
                )

            workspace = await self._client.workspace(self.settings.workspace_id)
            if str(workspace.get("id") or "") != self.settings.workspace_id:
                raise ClickClackError(
                    "The bot cannot access the configured ClickClack workspace"
                )

            owner_id = str(self._bot.get("owner_user_id") or "").strip()
            if owner_id and not self._effective_allowed_user_ids:
                self._effective_allowed_user_ids.add(owner_id)
            if (
                not self.settings.allow_all_users
                and not self._effective_allowed_user_ids
            ):
                raise ClickClackError(
                    "No allowed_user_ids configured and this is not a user-owned bot"
                )

            channels = await self._client.channels(self.settings.workspace_id)
            self._channel_names = {
                str(item.get("id")): str(
                    item.get("display_title") or item.get("name") or item.get("id")
                )
                for item in channels
                if item.get("id")
            }
            inaccessible = self.settings.allowed_channel_ids.difference(
                self._channel_names
            )
            if inaccessible:
                raise ClickClackError(
                    "Configured channel IDs are unavailable to the bot: "
                    + ", ".join(sorted(inaccessible))
                )

            self._cursor = await asyncio.to_thread(self._cursor_store.load)
            if (
                self._cursor is None
                and self.settings.skip_history_on_first_start
            ):
                self._cursor = await self._capture_tail_cursor()
                if self._cursor:
                    await asyncio.to_thread(
                        self._cursor_store.save, self._cursor
                    )
            elif self._cursor:
                await self._replay_events()

            self._ws = await self._open_websocket()
            self._receive_task = asyncio.create_task(
                self._receive_loop(), name="clickclack-realtime"
            )
            self._mark_connected()
            logger.info(
                "ClickClack connected as @%s to workspace %s",
                self._bot_handle,
                self.settings.workspace_id,
            )
            return True
        except Exception as exc:
            detail = _sanitize_error_text(str(exc), self.settings.token)
            logger.error("ClickClack connection failed: %s", detail)
            await self._close_network()
            self._release_token_lock()
            retryable = getattr(exc, "retryable", True)
            self._set_fatal_error("connect_failed", detail, retryable=retryable)
            return False

    async def disconnect(self) -> None:
        self._closing = True
        self._mark_disconnected()
        current = asyncio.current_task()
        if (
            self._receive_task
            and self._receive_task is not current
            and not self._receive_task.done()
        ):
            self._receive_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._receive_task
        self._receive_task = None
        await self._close_network()
        self._release_token_lock()
        logger.info("ClickClack disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        del reply_to
        if not content:
            return SendResult(success=True)
        if self._client is None:
            return SendResult(
                success=False,
                error="ClickClack is not connected",
                retryable=True,
            )

        try:
            route_type, route_id = _parse_chat_id(chat_id)
            thread_id = str((metadata or {}).get("thread_id") or "").strip()
            nonce = f"hermes-{uuid.uuid4().hex}"
            if thread_id:
                message = await self._client.create_thread_reply(
                    thread_id, content, nonce
                )
            elif route_type == "dm":
                message = await self._client.create_direct_message(
                    route_id, content, nonce
                )
            else:
                message = await self._client.create_channel_message(
                    route_id, content, nonce
                )
            message_id = str(message.get("id") or "")
            if not message_id:
                raise ClickClackError(
                    "ClickClack did not return a message ID", retryable=False
                )
            return SendResult(
                success=True, message_id=message_id, raw_response=message
            )
        except ClickClackError as exc:
            return SendResult(
                success=False,
                error=str(exc),
                retryable=exc.retryable,
                retry_after=exc.retry_after,
            )
        except Exception as exc:
            return SendResult(
                success=False,
                error=_sanitize_error_text(str(exc), self.settings.token),
                retryable=True,
            )

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        del chat_id, finalize
        if self._client is None:
            return SendResult(
                success=False,
                error="ClickClack is not connected",
                retryable=True,
            )
        try:
            message = await self._client.edit_message(message_id, content)
            return SendResult(
                success=True,
                message_id=str(message.get("id") or message_id),
                raw_response=message,
            )
        except ClickClackError as exc:
            return SendResult(
                success=False,
                error=str(exc),
                retryable=exc.retryable,
                retry_after=exc.retry_after,
            )

    async def send_typing(
        self, chat_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        del metadata
        if self._client is None:
            return
        try:
            route_type, route_id = _parse_chat_id(chat_id)
            await self._client.publish_typing(
                self.settings.workspace_id,
                channel_id=route_id if route_type == "channel" else None,
                direct_conversation_id=route_id if route_type == "dm" else None,
            )
        except Exception:
            logger.debug("ClickClack typing indicator failed", exc_info=True)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        try:
            route_type, route_id = _parse_chat_id(chat_id)
        except ValueError:
            return {"name": chat_id, "type": "channel"}
        if route_type == "dm":
            return {"name": route_id, "type": "dm"}
        return {
            "name": self._channel_names.get(route_id, route_id),
            "type": "channel",
        }

    async def _open_websocket(self) -> Any:
        if self._client is None:
            raise ClickClackError("ClickClack client is not initialized")
        return await websockets.connect(
            self._client.websocket_url(
                self.settings.workspace_id, self._cursor
            ),
            additional_headers={
                "Authorization": f"Bearer {self.settings.token}",
            },
            user_agent_header=f"clickclack-hermes-plugin/{PLUGIN_VERSION}",
            open_timeout=self.settings.request_timeout_seconds,
            close_timeout=10,
            ping_interval=20,
            ping_timeout=20,
            max_size=2 * 1024 * 1024,
        )

    async def _receive_loop(self) -> None:
        backoff = 1.0
        while not self._closing:
            try:
                if self._ws is None:
                    await self._replay_events()
                    self._ws = await self._open_websocket()
                    self._mark_connected()
                    logger.info("ClickClack realtime connection restored")
                async for raw_event in self._ws:
                    event = json.loads(
                        raw_event.decode("utf-8")
                        if isinstance(raw_event, bytes)
                        else raw_event
                    )
                    if isinstance(event, dict):
                        await self._consume_event(event)
                raise ClickClackError(
                    "ClickClack realtime connection closed", retryable=True
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._closing:
                    break
                detail = _sanitize_error_text(str(exc), self.settings.token)
                logger.warning(
                    "ClickClack realtime interrupted (%s); retrying in %.1fs",
                    detail,
                    backoff,
                )
                self._mark_disconnected()
                await self._close_websocket()
                await asyncio.sleep(backoff + random.random() * 0.25)
                backoff = min(
                    self.settings.reconnect_max_seconds, backoff * 2
                )
            else:
                backoff = 1.0

    async def _consume_event(self, event: Mapping[str, Any]) -> None:
        cursor = str(event.get("cursor") or "").strip()
        try:
            if (
                event.get("workspace_id") == self.settings.workspace_id
                and event.get("type") in MESSAGE_EVENT_TYPES
            ):
                await self._handle_realtime_message(event)
        finally:
            if cursor:
                self._cursor = cursor
                await asyncio.to_thread(self._cursor_store.save, cursor)

    async def _handle_realtime_message(
        self, event: Mapping[str, Any]
    ) -> None:
        if self._client is None:
            return
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            return
        message_id = str(payload.get("message_id") or "").strip()
        if not message_id:
            return
        message = await self._client.message(message_id)
        if not message or message.get("deleted_at"):
            return

        author = message.get("author")
        # Fail closed when the server doesn't provide an author object. This
        # prevents a bot-authored message from becoming a bot-to-bot loop.
        if not isinstance(author, Mapping):
            logger.warning(
                "Ignoring ClickClack message %s without author metadata",
                message_id,
            )
            return
        author_id = str(message.get("author_id") or author.get("id") or "")
        if (
            not author_id
            or author_id == self._bot_user_id
            or author.get("kind") == "bot"
        ):
            return
        if (
            not self.settings.allow_all_users
            and author_id not in self._effective_allowed_user_ids
        ):
            logger.debug(
                "Ignoring ClickClack message from unauthorized user %s",
                author_id,
            )
            return

        channel_id = str(message.get("channel_id") or "").strip()
        dm_id = str(message.get("direct_conversation_id") or "").strip()
        if channel_id:
            if (
                self.settings.allowed_channel_ids
                and channel_id not in self.settings.allowed_channel_ids
            ):
                return
            route_chat_id = f"channel:{channel_id}"
            chat_type = "group"
            chat_name = self._channel_names.get(channel_id, channel_id)
        elif dm_id and self.settings.allow_dms:
            route_chat_id = f"dm:{dm_id}"
            chat_type = "dm"
            chat_name = str(author.get("display_name") or author.get("handle") or dm_id)
        else:
            return

        text = str(message.get("body") or "").strip()
        if not text:
            return
        if channel_id and self.settings.require_mention:
            text, mentioned = _strip_mention(text, self._bot_handle)
            if not mentioned:
                return
            if not text:
                text = "请问我现在可以帮你做什么？"

        parent_message_id = str(message.get("parent_message_id") or "").strip()
        thread_root_id = str(message.get("thread_root_id") or "").strip()
        thread_id: str | None = None
        if self.settings.thread_mode == "always" and channel_id:
            thread_id = thread_root_id or message_id
        elif self.settings.thread_mode == "existing" and parent_message_id:
            thread_id = thread_root_id or parent_message_id

        source = self.build_source(
            chat_id=route_chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=author_id,
            user_name=str(
                author.get("display_name") or author.get("handle") or author_id
            ),
            thread_id=thread_id,
            scope_id=self.settings.workspace_id,
            parent_chat_id=route_chat_id if thread_id else None,
            message_id=message_id,
        )
        normalized_event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=message,
            message_id=message_id,
            reply_to_message_id=parent_message_id or None,
            timestamp=_parse_timestamp(message.get("created_at")),
            metadata={
                "clickclack_event_id": str(event.get("id") or ""),
                "clickclack_cursor": str(event.get("cursor") or ""),
                "clickclack_thread_root_id": thread_root_id or None,
            },
        )
        await self.handle_message(normalized_event)

    async def _capture_tail_cursor(self) -> str | None:
        if self._client is None:
            return None
        page = await self._client.events(
            self.settings.workspace_id, limit=1, include_tail=True
        )
        tail = str(page.get("tail_cursor") or "").strip()
        if tail:
            return tail

        # Compatibility fallback for a ClickClack server that predates
        # include_tail. Drain only event metadata until the current end.
        cursor: str | None = None
        for _ in range(100):
            page = await self._client.events(
                self.settings.workspace_id,
                after_cursor=cursor,
                limit=200,
            )
            events = page.get("events")
            if not isinstance(events, list) or not events:
                return cursor
            next_cursor = str(events[-1].get("cursor") or "").strip()
            if not next_cursor or next_cursor == cursor:
                return cursor
            cursor = next_cursor
            if len(events) < 200:
                return cursor
        raise ClickClackError(
            "Could not find the current ClickClack event tail", retryable=True
        )

    async def _replay_events(self) -> None:
        if self._client is None:
            return
        for _ in range(100):
            page = await self._client.events(
                self.settings.workspace_id,
                after_cursor=self._cursor,
                limit=200,
            )
            events = page.get("events")
            if not isinstance(events, list) or not events:
                return
            for event in events:
                if isinstance(event, Mapping):
                    await self._consume_event(event)
            if len(events) < 200:
                return
        raise ClickClackError(
            "ClickClack event replay exceeded 20,000 events", retryable=True
        )

    async def _close_websocket(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            with suppress(Exception):
                await ws.close()

    async def _close_network(self) -> None:
        await self._close_websocket()
        client, self._client = self._client, None
        if client is not None:
            with suppress(Exception):
                await client.close()

    def _acquire_token_lock(self) -> bool:
        if self._lock_key:
            return True
        fingerprint = hashlib.sha256(
            self.settings.token.encode("utf-8")
        ).hexdigest()[:24]
        base_lock = getattr(self, "_acquire_platform_lock", None)
        if callable(base_lock):
            if not base_lock(
                "clickclack", fingerprint, "ClickClack bot token"
            ):
                return False
            self._lock_key = fingerprint
            return True
        try:
            from gateway.status import acquire_scoped_lock

            result = acquire_scoped_lock("clickclack", fingerprint)
            acquired = result[0] if isinstance(result, tuple) else bool(result)
            if not acquired:
                detail = (
                    "This ClickClack bot token is already used by another "
                    "Hermes profile or gateway"
                )
                logger.error(detail)
                self._set_fatal_error(
                    "lock_conflict", detail, retryable=False
                )
                return False
            self._lock_key = fingerprint
        except ImportError:
            self._lock_key = "unavailable"
        return True

    def _release_token_lock(self) -> None:
        lock_key, self._lock_key = self._lock_key, None
        if not lock_key or lock_key == "unavailable":
            return
        base_release = getattr(self, "_release_platform_lock", None)
        if callable(base_release):
            base_release()
            return
        try:
            from gateway.status import release_scoped_lock

            release_scoped_lock("clickclack", lock_key)
        except Exception:
            pass


def check_requirements() -> bool:
    """Return whether the secret and Hermes-shipped dependencies are present."""
    return bool(os.getenv("CLICKCLACK_BOT_TOKEN", "").strip())


def validate_config(config: Any) -> bool:
    """Validate the minimum runtime configuration without making a request."""
    try:
        return not ClickClackSettings.from_config(config).missing_fields()
    except (TypeError, ValueError):
        return False


def register(ctx: Any) -> None:
    """Hermes plugin entry point."""
    ctx.register_platform(
        name="clickclack",
        label="ClickClack",
        adapter_factory=lambda cfg: ClickClackAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["CLICKCLACK_BOT_TOKEN"],
        install_hint=(
            "Set CLICKCLACK_BOT_TOKEN and configure "
            "gateway.platforms.clickclack.extra in ~/.hermes/config.yaml"
        ),
        max_message_length=0,
        emoji="🦞",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are chatting in ClickClack, which supports GitHub-flavored "
            "Markdown and one-level threads. Keep work for one issue inside "
            "its ClickClack thread. Do not assume messages in other threads "
            "belong to this session."
        ),
    )


__all__ = [
    "ClickClackAdapter",
    "ClickClackClient",
    "ClickClackError",
    "ClickClackSettings",
    "CursorStore",
    "register",
]
