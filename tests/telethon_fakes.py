"""Фейки Telethon для тестов Reader — без сети (§10 промпта пакета 2).

Сообщения — настоящие `telethon.tl.types.Message` (только так поля вроде
`grouped_id`/`fwd_from`/`noforwards` ведут себя как в реальном потоке),
клиент — свой фейк, реализующий структурно тот же протокол, что и
`TelegramClientLike` в `tg_monitor.reader`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from telethon.tl.types import Message, MessageFwdHeader


def make_message(
    message_id: int,
    *,
    date: dt.datetime,
    text: str | None = None,
    grouped_id: int | None = None,
    is_repost: bool = False,
    has_media: bool = False,
    forward_forbidden: bool = False,
) -> Message:
    return Message(
        id=message_id,
        peer_id=None,
        date=date,
        message=text,
        grouped_id=grouped_id,
        fwd_from=MessageFwdHeader(date=date) if is_repost else None,
        media="fake-media" if has_media else None,
        noforwards=forward_forbidden,
    )


class FakeEvent:
    """Минимальный аналог telethon.events.NewMessage.Event — только .message нужен Reader."""

    def __init__(self, message: Message) -> None:
        self.message = message


class FakeClient:
    """Фейк TelegramClient: без сети, история и entity задаются тестом напрямую."""

    def __init__(self) -> None:
        self.connected = False
        self.entities: dict[str, Any] = {}
        self.unresolvable: set[str] = set()
        self.get_entity_error: dict[str, Exception] = {}
        self.history: dict[str, list[Message]] = {}
        self.iter_error: dict[str, Exception] = {}
        self.handlers: dict[str, list[tuple[Callable[[Any], Awaitable[None]], Any]]] = {}
        self.entity_to_source: dict[str, str] = {}

    async def connect(self) -> None:
        self.connected = True

    async def get_entity(self, ref: str) -> Any:
        error = self.get_entity_error.pop(ref, None)
        if error is not None:
            raise error
        if ref in self.unresolvable:
            raise ValueError(f"Cannot find any entity corresponding to {ref!r}")
        return self.entities[ref]

    def iter_messages(self, entity: Any, *, min_id: int, reverse: bool) -> AsyncIterator[Message]:
        assert reverse is True
        source_id = self.entity_to_source[entity]
        return self._iter_history(source_id, min_id)

    async def _iter_history(self, source_id: str, min_id: int) -> AsyncIterator[Message]:
        error = self.iter_error.pop(source_id, None)
        if error is not None:
            raise error
        for message in self.history.get(source_id, []):
            if message.id > min_id:
                yield message

    def add_event_handler(self, callback: Callable[[Any], Awaitable[None]], event: Any) -> None:
        chats = event.chats
        entity = chats[0] if isinstance(chats, list) else chats
        source_id = self.entity_to_source[entity]
        self.handlers.setdefault(source_id, []).append((callback, event))

    def remove_event_handler(self, callback: Callable[[Any], Awaitable[None]], event: Any) -> None:
        for source_id, registered in self.handlers.items():
            self.handlers[source_id] = [
                (cb, ev) for cb, ev in registered if not (cb == callback and ev == event)
            ]

    async def run_until_disconnected(self) -> None:
        return None

    async def fire_new_message(self, source_id: str, message: Message) -> None:
        for callback, _event in self.handlers.get(source_id, []):
            await callback(FakeEvent(message))
