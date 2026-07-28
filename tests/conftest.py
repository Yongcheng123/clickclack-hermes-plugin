"""Small Hermes API stubs used by the standalone plugin test suite."""

from __future__ import annotations

import dataclasses
import enum
import sys
import types
from pathlib import Path
from typing import Any

gateway = types.ModuleType("gateway")
gateway.__path__ = []
gateway_config = types.ModuleType("gateway.config")
gateway_platforms = types.ModuleType("gateway.platforms")
gateway_platforms.__path__ = []
gateway_base = types.ModuleType("gateway.platforms.base")
hermes_constants = types.ModuleType("hermes_constants")


class Platform(enum.StrEnum):
    CLICKCLACK = "clickclack"

    @classmethod
    def _missing_(cls, value: object) -> Platform:
        member = str.__new__(cls, str(value))
        member._name_ = str(value).upper()
        member._value_ = str(value)
        return member


class MessageType(enum.Enum):
    TEXT = "text"


@dataclasses.dataclass
class Source:
    platform: Platform
    chat_id: str
    chat_name: str | None = None
    chat_type: str = "dm"
    user_id: str | None = None
    user_name: str | None = None
    thread_id: str | None = None
    scope_id: str | None = None
    parent_chat_id: str | None = None
    message_id: str | None = None


@dataclasses.dataclass
class MessageEvent:
    text: str
    message_type: MessageType
    source: Source
    raw_message: Any = None
    message_id: str | None = None
    reply_to_message_id: str | None = None
    timestamp: Any = None
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class SendResult:
    success: bool
    message_id: str | None = None
    error: str | None = None
    raw_response: Any = None
    retryable: bool = False
    retry_after: float | None = None


class BasePlatformAdapter:
    def __init__(self, config: Any, platform: Platform) -> None:
        self.config = config
        self.platform = platform
        self.events: list[MessageEvent] = []
        self.connected = False
        self.fatal_error: tuple[Any, ...] | None = None

    def build_source(self, **kwargs: Any) -> Source:
        return Source(platform=self.platform, **kwargs)

    async def handle_message(self, event: MessageEvent) -> None:
        self.events.append(event)

    def _mark_connected(self) -> None:
        self.connected = True

    def _mark_disconnected(self) -> None:
        self.connected = False

    def _set_fatal_error(
        self, kind: str, detail: str, *, retryable: bool
    ) -> None:
        self.fatal_error = (kind, detail, retryable)


gateway_config.Platform = Platform
gateway_base.BasePlatformAdapter = BasePlatformAdapter
gateway_base.MessageEvent = MessageEvent
gateway_base.MessageType = MessageType
gateway_base.SendResult = SendResult
hermes_constants.get_hermes_home = lambda: Path.cwd() / ".test-hermes"

sys.modules.setdefault("gateway", gateway)
sys.modules.setdefault("gateway.config", gateway_config)
sys.modules.setdefault("gateway.platforms", gateway_platforms)
sys.modules.setdefault("gateway.platforms.base", gateway_base)
sys.modules.setdefault("hermes_constants", hermes_constants)
