"""Фейки Telethon для тестов Reader — без сети (§10 промпта пакета 2).

Сообщения — настоящие `telethon.tl.types.Message` (только так поля вроде
`grouped_id`/`fwd_from`/`noforwards` ведут себя как в реальном потоке),
клиент — свой фейк, реализующий структурно тот же протокол, что и
`TelegramClientLike` в `tg_monitor.reader`.
"""

from __future__ import annotations

import asyncio
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
        # По умолчанию run_until_disconnected возвращается сразу (нужно
        # большинству тестов). test-ы Reader.run()/graceful shutdown
        # включают блокировку явно, имитируя реальный Telethon-клиент,
        # который висит в run_until_disconnected до disconnect().
        self.block_until_disconnected = False
        self._disconnected = asyncio.Event()

    async def connect(self) -> None:
        self.connected = True

    async def get_entity(self, ref: str) -> Any:
        error = self.get_entity_error.pop(ref, None)
        if error is not None:
            raise error
        if ref in self.unresolvable:
            raise ValueError(f"Cannot find any entity corresponding to {ref!r}")
        return self.entities[ref]

    def iter_messages(
        self, entity: Any, *, min_id: int = 0, reverse: bool, limit: int | None = None
    ) -> AsyncIterator[Message]:
        source_id = self.entity_to_source[entity]
        if reverse:
            return self._iter_history(source_id, min_id)
        return self._iter_latest(source_id, limit)

    async def _iter_history(self, source_id: str, min_id: int) -> AsyncIterator[Message]:
        error = self.iter_error.pop(source_id, None)
        if error is not None:
            raise error
        for message in self.history.get(source_id, []):
            if message.id > min_id:
                yield message

    async def _iter_latest(self, source_id: str, limit: int | None) -> AsyncIterator[Message]:
        # Реальный iter_messages(reverse=False) отдаёт сообщения от новых к
        # старым — для фейка достаточно развернуть историю (она хранится в
        # хронологическом порядке) и обрезать по limit.
        error = self.iter_error.pop(source_id, None)
        if error is not None:
            raise error
        newest_first = list(reversed(self.history.get(source_id, [])))
        if limit is not None:
            newest_first = newest_first[:limit]
        for message in newest_first:
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
        if self.block_until_disconnected:
            await self._disconnected.wait()

    async def disconnect(self) -> None:
        self._disconnected.set()

    async def fire_new_message(self, source_id: str, message: Message) -> None:
        for callback, _event in self.handlers.get(source_id, []):
            await callback(FakeEvent(message))
