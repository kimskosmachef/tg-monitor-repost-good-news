"""Reader — Telethon-клиент, §3, §7, §9 docs/spec.md.

Единственный компонент, знающий про Telegram API чтения. Подписывается на
`events.NewMessage` по источникам со статусом `active`, периодически
добирает историю по `last_message_id` (и в штатном режиме, и после
разрыва соединения — отдельного хука на реконнект нет: Telethon сам
восстанавливает транспорт, а периодический добор одинаково закрывает
любой разрыв в потоке, откуда бы он ни взялся). Публикации ещё нет
(пакет 5), поэтому нормализованные посты уходят в `Sink`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from telethon import errors, events

from tg_monitor.config_store import ConfigStore
from tg_monitor.models import Post, Source
from tg_monitor.sources_registry import mark_source_unavailable
from tg_monitor.state import StateStore

logger = logging.getLogger(__name__)

# Источник недоступен или аккаунт из него исключён — §9. ValueError — так
# Telethon сигнализирует, что get_entity не смог разрешить ref (например,
# username больше не существует).
_UNAVAILABLE_ERRORS: tuple[type[Exception], ...] = (
    errors.ChannelPrivateError,
    errors.ChannelInvalidError,
    errors.UsernameInvalidError,
    errors.UserBannedInChannelError,
    errors.ChatIdInvalidError,
    ValueError,
)


class Sink(Protocol):
    """Приёмник нормализованных постов. Publisher (пакет 5) реализует этот интерфейс."""

    async def handle(self, post: Post) -> None: ...


class LoggingSink:
    """Sink для режима наблюдения (пакет 2): печатает пост в лог, никуда не публикует."""

    def __init__(self, log: logging.Logger | None = None) -> None:
        self._logger = log or logger

    async def handle(self, post: Post) -> None:
        self._logger.info(
            "post source=%s message_id=%s date=%s repost=%s media=%s forward_forbidden=%s text=%r",
            post.source_id,
            post.message_id,
            post.date.isoformat(),
            post.is_repost,
            post.has_media,
            post.forward_forbidden,
            post.text,
        )


class TelegramClientLike(Protocol):
    """Минимальный интерфейс Telethon-клиента, которым пользуется Reader.

    В тестах подменяется фейком (без сети) — реальный `TelegramClient`
    удовлетворяет этому протоколу структурно, без адаптера.
    """

    async def connect(self) -> None: ...

    async def get_entity(self, ref: str) -> Any: ...

    def iter_messages(self, entity: Any, *, min_id: int, reverse: bool) -> Any: ...

    def add_event_handler(self, callback: Callable[[Any], Awaitable[None]], event: Any) -> None: ...

    def remove_event_handler(
        self, callback: Callable[[Any], Awaitable[None]], event: Any
    ) -> None: ...

    async def run_until_disconnected(self) -> None: ...


def _text_of(message: Any) -> str | None:
    text: str | None = getattr(message, "message", None)
    return text or None


def _has_media(message: Any) -> bool:
    return getattr(message, "media", None) is not None


def _is_repost(message: Any) -> bool:
    return getattr(message, "fwd_from", None) is not None


def _forward_forbidden(message: Any) -> bool:
    return bool(getattr(message, "noforwards", False))


def _pick_representative(messages: list[Any]) -> Any:
    # §7: у медиагруппы оценивается подпись группы — берём элемент с
    # непустым текстом, он один на альбом. Если подписи нет вовсе (чистый
    # альбом без текста) — берём первый пришедший элемент.
    for message in messages:
        if _text_of(message):
            return message
    return messages[0]


def _build_post(source_id: str, messages: list[Any], tz: ZoneInfo) -> Post:
    representative = _pick_representative(messages)
    grouped_id = getattr(representative, "grouped_id", None)
    return Post(
        message_id=representative.id,
        source_id=source_id,
        date=representative.date.astimezone(tz),
        text=_text_of(representative),
        grouped_id=grouped_id,
        is_repost=_is_repost(representative),
        has_media=_has_media(representative) or grouped_id is not None,
        forward_forbidden=_forward_forbidden(representative),
    )


class TelegramReader:
    """Читает источники, собирает медиагруппы, добирает историю, кормит Sink."""

    def __init__(
        self,
        client: TelegramClientLike,
        config_store: ConfigStore,
        state_store: StateStore,
        sink: Sink,
        log: logging.Logger | None = None,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._config_store = config_store
        self._state_store = state_store
        self._sink = sink
        self._logger = log or logger
        self._now = now
        self._sleeper = sleeper

        self._state = state_store.load()
        self._entities: dict[str, Any] = {}
        self._live_handlers: dict[str, tuple[Callable[[Any], Awaitable[None]], Any]] = {}
        self._source_locks: dict[str, asyncio.Lock] = {}
        self._live_group_buffers: dict[tuple[str, int], list[Any]] = {}
        self._live_flush_tasks: dict[tuple[str, int], asyncio.Task[None]] = {}

    async def run(self) -> None:
        """Подключиться, подписаться на активные источники, запустить цикл добора."""
        await self._client.connect()
        bundle = self._config_store.get()
        await self._subscribe_active_sources(bundle.sources)

        catchup_task = asyncio.create_task(self._catchup_loop())
        try:
            await self._client.run_until_disconnected()
        finally:
            catchup_task.cancel()

    async def _subscribe_active_sources(self, sources: list[Source]) -> None:
        for source in sources:
            if source.status != "active" or source.id in self._entities:
                continue
            entity = await self._resolve_entity(source)
            if entity is None:
                continue
            self._entities[source.id] = entity
            callback = functools.partial(self._on_event, source.id)
            event_filter = events.NewMessage(chats=entity)
            self._client.add_event_handler(callback, event_filter)
            self._live_handlers[source.id] = (callback, event_filter)

    async def _resolve_entity(self, source: Source) -> Any | None:
        while True:
            try:
                return await self._client.get_entity(source.ref)
            except errors.FloodWaitError as exc:
                self._logger.warning(
                    "FloodWait при резолве источника %s: ожидание %s секунд",
                    source.id,
                    exc.seconds,
                )
                await self._sleeper(exc.seconds)
            except _UNAVAILABLE_ERRORS as exc:
                self._logger.error(
                    "источник %s недоступен (%s), подписка пропущена", source.id, exc
                )
                bundle = self._config_store.get()
                mark_source_unavailable(bundle.sources_path, source.id, self._logger)
                return None

    async def _on_event(self, source_id: str, event: Any) -> None:
        await self._handle_incoming(source_id, event.message)

    async def _handle_incoming(self, source_id: str, message: Any) -> None:
        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id is None:
            await self._process_batch(source_id, [message])
            return
        key = (source_id, grouped_id)
        self._live_group_buffers.setdefault(key, []).append(message)
        if key not in self._live_flush_tasks:
            self._live_flush_tasks[key] = asyncio.create_task(self._flush_live_group(key))

    async def _flush_live_group(self, key: tuple[str, int]) -> None:
        bundle = self._config_store.get()
        await self._sleeper(bundle.config.runtime.media_group_flush_delay_sec)
        messages = self._live_group_buffers.pop(key, [])
        self._live_flush_tasks.pop(key, None)
        if messages:
            await self._process_batch(key[0], messages)

    async def _process_batch(self, source_id: str, messages: list[Any]) -> None:
        lock = self._source_locks.setdefault(source_id, asyncio.Lock())
        async with lock:
            await self._emit_batch(source_id, messages)

    async def _emit_batch(self, source_id: str, messages: list[Any]) -> None:
        bundle = self._config_store.get()
        tz = ZoneInfo(bundle.config.logging.timezone)
        post = _build_post(source_id, messages, tz)
        max_id = max(message.id for message in messages)

        age_cutoff = self._now() - dt.timedelta(minutes=bundle.config.runtime.max_post_age_min)
        if post.date < age_cutoff:
            # §7: старьё после долгого простоя не отдаётся дальше, но
            # помечается обработанным — молчаливой потери нет (CLAUDE.md).
            self._logger.info(
                "пост старше max_post_age_min, помечен обработанным без передачи в sink: "
                "source=%s message_id=%s date=%s",
                source_id,
                post.message_id,
                post.date.isoformat(),
            )
        else:
            await self._sink.handle(post)

        # §7: last_message_id продвигается только после передачи в sink
        # (или после явного решения не передавать — ветка выше).
        current = self._state.last_message_id.get(source_id, 0)
        if max_id > current:
            self._state.last_message_id[source_id] = max_id
            self._state_store.save(self._state)

    async def _catchup_loop(self) -> None:
        while True:
            bundle = self._config_store.get()
            await self._subscribe_active_sources(bundle.sources)
            for source_id, entity in list(self._entities.items()):
                await self._catchup_source(source_id, entity)
            bundle = self._config_store.get()
            await self._sleeper(bundle.config.runtime.catchup_interval_min * 60)

    async def _catchup_source(self, source_id: str, entity: Any) -> None:
        lock = self._source_locks.setdefault(source_id, asyncio.Lock())
        async with lock:
            buffer: list[Any] = []
            while True:
                min_id = self._state.last_message_id.get(source_id, 0)
                try:
                    async for message in self._client.iter_messages(
                        entity, min_id=min_id, reverse=True
                    ):
                        message_group = getattr(message, "grouped_id", None)
                        buffer_group = getattr(buffer[0], "grouped_id", None) if buffer else None
                        if buffer and message_group != buffer_group:
                            await self._emit_batch(source_id, buffer)
                            buffer = []
                        buffer.append(message)
                        if message_group is None:
                            await self._emit_batch(source_id, buffer)
                            buffer = []
                    if buffer:
                        await self._emit_batch(source_id, buffer)
                        buffer = []
                    return
                except errors.FloodWaitError as exc:
                    self._logger.warning(
                        "FloodWait при доборе истории source=%s: ожидание %s секунд",
                        source_id,
                        exc.seconds,
                    )
                    buffer = []
                    await self._sleeper(exc.seconds)
                except _UNAVAILABLE_ERRORS as exc:
                    self._logger.error(
                        "источник %s недоступен при доборе истории (%s), пропуск",
                        source_id,
                        exc,
                    )
                    self._mark_unavailable(source_id)
                    return

    def _mark_unavailable(self, source_id: str) -> None:
        self._entities.pop(source_id, None)
        handler_info = self._live_handlers.pop(source_id, None)
        if handler_info is not None:
            callback, event_filter = handler_info
            self._client.remove_event_handler(callback, event_filter)
        bundle = self._config_store.get()
        mark_source_unavailable(bundle.sources_path, source_id, self._logger)
        # TODO(пакет 6): уведомление в служебный канал (service_chat).
