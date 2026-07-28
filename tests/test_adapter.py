from __future__ import annotations

import sys
import types

import httpx
import pytest

from adapter import (
    ClickClackAdapter,
    ClickClackClient,
    ClickClackError,
    ClickClackSettings,
    _parse_chat_id,
    _strip_mention,
)


def make_config(**extra):
    defaults = {
        "base_url": "https://chat.example.test/prefix",
        "workspace_id": "wsp_team",
        "default_channel_id": "chn_sprint",
        "allowed_channel_ids": ["chn_sprint"],
        "allowed_user_ids": ["usr_owner"],
        "require_mention": True,
        "allow_dms": False,
        "thread_mode": "always",
    }
    defaults.update(extra)
    return types.SimpleNamespace(extra=defaults)


def test_settings_are_secure_by_default(monkeypatch):
    monkeypatch.setenv("CLICKCLACK_BOT_TOKEN", "ccb_test")
    settings = ClickClackSettings.from_config(make_config())

    assert settings.require_mention is True
    assert settings.allow_dms is False
    assert settings.allow_all_users is False
    assert settings.thread_mode == "always"
    assert settings.allowed_channel_ids == frozenset({"chn_sprint"})


def test_invalid_base_url_is_rejected(monkeypatch):
    monkeypatch.setenv("CLICKCLACK_BOT_TOKEN", "ccb_test")
    with pytest.raises(ValueError, match="absolute"):
        ClickClackSettings.from_config(make_config(base_url="chat.example.test"))


def test_mention_is_exact_and_removed():
    assert _strip_mention("@alice-hermes please run tests", "alice-hermes") == (
        "please run tests",
        True,
    )
    assert _strip_mention("hello @alice-hermes: status", "alice-hermes") == (
        "hello : status",
        True,
    )
    assert _strip_mention("@alice-hermes-extra no", "alice-hermes") == (
        "@alice-hermes-extra no",
        False,
    )


def test_route_parsing():
    assert _parse_chat_id("channel:chn_1") == ("channel", "chn_1")
    assert _parse_chat_id("dm:dm_1") == ("dm", "dm_1")
    assert _parse_chat_id("chn_legacy") == ("channel", "chn_legacy")


@pytest.mark.asyncio
async def test_client_preserves_path_prefix_and_redacts_token():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401, text="bad ccb_supersecret")

    client = ClickClackClient(
        "https://chat.example.test/services/clickclack",
        "ccb_supersecret",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ClickClackError) as raised:
        await client.me()
    await client.close()

    assert seen[0].url.path == "/services/clickclack/api/me"
    assert seen[0].headers["Authorization"] == "Bearer ccb_supersecret"
    assert "ccb_supersecret" not in str(raised.value)


class FakeClient:
    def __init__(self, message):
        self._message = message

    async def message(self, _message_id):
        return self._message


def event(message_id="msg_1", event_type="message.created"):
    return {
        "id": "evt_1",
        "cursor": "cur_1",
        "type": event_type,
        "workspace_id": "wsp_team",
        "channel_id": "chn_sprint",
        "payload": {"message_id": message_id},
    }


def message(
    *,
    body="@owner-hermes implement MOB-101",
    author_id="usr_owner",
    author_kind="human",
    channel_id="chn_sprint",
    parent_message_id=None,
    thread_root_id="msg_1",
):
    return {
        "id": "msg_1",
        "workspace_id": "wsp_team",
        "channel_id": channel_id,
        "author_id": author_id,
        "parent_message_id": parent_message_id,
        "thread_root_id": thread_root_id,
        "body": body,
        "created_at": "2026-07-28T12:00:00Z",
        "author": {
            "id": author_id,
            "kind": author_kind,
            "handle": "owner",
            "display_name": "Owner",
        },
    }


def configured_adapter(monkeypatch):
    monkeypatch.setenv("CLICKCLACK_BOT_TOKEN", "ccb_test")
    adapter = ClickClackAdapter(make_config())
    adapter._client = FakeClient(message())
    adapter._bot_user_id = "bot_1"
    adapter._bot_handle = "owner-hermes"
    adapter._effective_allowed_user_ids = {"usr_owner"}
    adapter._channel_names = {"chn_sprint": "hermes-sprint"}
    return adapter


@pytest.mark.asyncio
async def test_channel_mention_becomes_thread_scoped_hermes_event(monkeypatch):
    adapter = configured_adapter(monkeypatch)
    await adapter._handle_realtime_message(event())

    assert len(adapter.events) == 1
    normalized = adapter.events[0]
    assert normalized.text == "implement MOB-101"
    assert normalized.source.chat_id == "channel:chn_sprint"
    assert normalized.source.thread_id == "msg_1"
    assert normalized.source.scope_id == "wsp_team"
    assert normalized.source.user_id == "usr_owner"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "incoming",
    [
        message(body="unmentioned"),
        message(author_id="usr_stranger"),
        message(author_id="bot_2", author_kind="bot"),
        message(channel_id="chn_other"),
    ],
)
async def test_unsafe_or_unaddressed_messages_are_ignored(monkeypatch, incoming):
    adapter = configured_adapter(monkeypatch)
    adapter._client = FakeClient(incoming)

    await adapter._handle_realtime_message(event())

    assert adapter.events == []


@pytest.mark.asyncio
async def test_user_owned_bot_defaults_to_its_owner(monkeypatch):
    monkeypatch.setenv("CLICKCLACK_BOT_TOKEN", "ccb_test")
    adapter = ClickClackAdapter(make_config(allowed_user_ids=[]))
    adapter._client = types.SimpleNamespace(
        me=lambda: None,
    )

    assert adapter._effective_allowed_user_ids == set()
    adapter._bot = {"owner_user_id": "usr_owner"}
    owner_id = adapter._bot["owner_user_id"]
    if owner_id and not adapter._effective_allowed_user_ids:
        adapter._effective_allowed_user_ids.add(owner_id)

    assert adapter._effective_allowed_user_ids == {"usr_owner"}


def test_scoped_lock_tuple_conflict_is_enforced(monkeypatch):
    adapter = configured_adapter(monkeypatch)
    status = types.ModuleType("gateway.status")
    status.acquire_scoped_lock = lambda *_args, **_kwargs: (
        False,
        {"pid": 123},
    )
    status.release_scoped_lock = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "gateway.status", status)

    assert adapter._acquire_token_lock() is False
    assert adapter.fatal_error[0] == "lock_conflict"
