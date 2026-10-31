"""Bounded opt-in delta sessions; Codex full-history requests remain stateless."""
from __future__ import annotations

import re
import time
import uuid
import threading
from dataclasses import dataclass

from .core import BridgeError

_SESSION_KEY = re.compile(r"^[A-Za-z0-9_-]{8,72}$")


@dataclass
class SessionRecord:
    session_id: str
    model: str
    last_used: float
    turns: int = 0
    reported_input_tokens: int = 0


class SessionManager:
    def __init__(self, max_turns: int = 4, max_input_tokens: int = 24000, ttl: float = 900.0):
        self.max_turns = max_turns
        self.max_input_tokens = max_input_tokens
        self.ttl = ttl
        self._records: dict[str, SessionRecord] = {}
        self._busy: set[str] = set()
        self._lock = threading.Lock()

    def choose(self, key: str | None, model: str, *, is_delta: bool) -> tuple[str | None, bool]:
        if not key:
            return None, False
        if not _SESSION_KEY.fullmatch(key):
            raise BridgeError("bad_session", "增量会话标识格式不正确", 422)
        if not is_delta:
            raise BridgeError("bad_session", "只有单条增量消息可以复用 SDK Session", 422)
        with self._lock:
            if key in self._busy:
                raise BridgeError("session_busy", "同一增量会话已有请求正在执行", 409)
            now = time.monotonic()
            record = self._records.get(key)
            rotated = (record is None or record.model != model or now - record.last_used > self.ttl
                       or record.turns >= self.max_turns
                       or record.reported_input_tokens >= self.max_input_tokens)
            if rotated:
                record = SessionRecord(str(uuid.uuid4()), model, now)
                self._records[key] = record
            record.last_used = now
            self._busy.add(key)
            return record.session_id, rotated

    def record(self, key: str | None, usage: dict[str, int]) -> None:
        if not key:
            return
        with self._lock:
            record = self._records.get(key)
            if record is not None:
                record.turns += 1
                record.reported_input_tokens = max(record.reported_input_tokens,
                                                   usage.get("input_tokens", 0))

    def release(self, key: str | None) -> None:
        if key:
            with self._lock:
                self._busy.discard(key)


def is_single_delta(value: object) -> bool:
    if isinstance(value, str):
        return True
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        item = value[0]
        return item.get("type") in (None, "message") and item.get("role") == "user"
    return False
