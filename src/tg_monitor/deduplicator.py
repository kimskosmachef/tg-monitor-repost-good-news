"""Deduplicator — отсекает повторную публикацию той же новости, §3, §6 docs/spec.md.

Встаёт между `MatchingSink` (§5, пакет 3) и приёмником (в этом пакете —
по-прежнему `LoggingSink`, публикации ещё нет — пакет 5). Хранит кольцевой
буфер векторов опубликованного за `runtime.dedup_window_hours`, раздельно по
темам: §6 требует, чтобы одинаковые посты в разных темах шли в разные каналы
и не глушили друг друга.

Вектор для сравнения не пересчитывается: `MatchingSink` передаёт сюда
`MatchResult` уже с вектором чанка, давшим максимальный score победившей
грани (см. `matcher._winning_chunk_vector`) — тем самым чанком, который решил
прохождение темы. Он же самое точное представление "о чём этот пост для этой
темы", доступное без повторного обращения к эмбеддеру.

§6 v2.3: проверка (`check`) и фиксация (`commit`) — разные операции. `check`
только читает буфер и решает, дубль пост или нет. `commit` добавляет векторы
победивших результатов в буфер и обязана вызываться лишь после подтверждённой
публикации — иначе пост, прошедший дедуп, но не опубликованный (запрет
пересылки, ошибка отправки, снятие по лимиту), заглушит следующий настоящий
дубль той же новости. Publisher'а пока нет (пакет 5), поэтому в `handle` роль
"подтверждения публикации" играет приёмник (`Sink`): он получает `post` и
функцию `commit` и обязан вызвать её сам, только если публикация состоялась.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from typing import Protocol
from zoneinfo import ZoneInfo

import numpy as np

from tg_monitor.config_store import ConfigStore
from tg_monitor.embedder import Vector
from tg_monitor.matcher import MatchResult
from tg_monitor.models import Post
from tg_monitor.reader import LoggingSink
from tg_monitor.state import DedupBufferData, DedupBufferStore, DedupEntry

logger = logging.getLogger(__name__)


class Sink(Protocol):
    """Приёмник постов, переживших дедуп.

    В отличие от `reader.Sink` получает вторым аргументом `commit` — функцию
    без аргументов, которую приёмник обязан вызвать сам, и только если пост
    в итоге опубликован (§6 v2.3). Не вызвал — вектор в буфер дедупа не
    попадёт, и следующий такой же пост дублем считаться не будет. В пакете 5
    эту роль займёт Publisher, вызывая `commit` на успешный форвард; здесь —
    временная заглушка (логирующий приёмник), которая считает публикацию
    состоявшейся безусловно.
    """

    async def handle(self, post: Post, commit: Callable[[], None]) -> None: ...


class CommittingLoggingSink:
    """Заглушка на месте Publisher (пакет 5, §6 v2.3).

    Оборачивает `reader.LoggingSink`: логирует пост как раньше и сразу же
    вызывает `commit` — публикация в режиме наблюдения считается состоявшейся
    безусловно. В пакете 5 эту роль займёт настоящий Publisher: он вызовет
    `commit` только на успешный форвард (не на любую попытку), поэтому
    поведение здесь — временное и заведомо более оптимистичное.
    """

    def __init__(self, log: logging.Logger | None = None, tz: ZoneInfo | None = None) -> None:
        self._inner = LoggingSink(log, tz)

    async def handle(self, post: Post, commit: Callable[[], None]) -> None:
        await self._inner.handle(post)
        commit()


def _cosine(a: Vector, b: Vector) -> float:
    # Векторы Embedder'а нормализованы (unit-length) — косинус это скалярное
    # произведение, как и в matcher._cosine.
    return float(np.dot(a, b))


class Deduplicator:
    """Кольцевой буфер векторов опубликованного, раздельный по темам — §6.

    Буфер живёт в `dedup-buffer.json` (`DedupBufferData`, §8) — отдельно от
    `state.json`, который держит `last_message_id` и версии центроидов.
    Разделение не косметическое: `state.json` переписывается на каждый пост
    и должен оставаться маленьким, а буфер дедупа — это сотни векторов,
    единицы мегабайт, и пишется он только когда меняется составом (см.
    `commit`), а не на каждый обработанный пост.
    """

    def __init__(
        self,
        config_store: ConfigStore,
        buffer_store: DedupBufferStore,
        buffer: DedupBufferData,
        sink: Sink,
        log: logging.Logger | None = None,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self._config_store = config_store
        self._buffer_store = buffer_store
        self._buffer = buffer
        self._sink = sink
        self._logger = log or logger
        self._now = now

        # §8, пункт 4 промпта: записи старше окна вычищаются уже при загрузке,
        # не только по ходу работы — иначе состояние, накопленное до долгого
        # простоя, раздувало бы буфер до первого же прошедшего поста. Сам
        # прунинг при старте на диск не пишется — файл меняется только когда
        # пост проходит тему (§8 v2.2, пункт 2 промпта), а не из-за одной
        # лишь чистки просроченных записей.
        bundle = self._config_store.get()
        window = dt.timedelta(hours=bundle.config.runtime.dedup_window_hours)
        self._prune(self._now(), window)

    async def handle(self, post: Post, results: list[MatchResult]) -> None:
        """Проверить `results` на дубли и передать выжившее приёмнику, который
        сам решает, публиковать ли пост и, если да — вызвать `commit`.

        §9, пункт 7 промпта: сбой дедупа не должен ронять обработку и не
        должен отбрасывать пост — пропустить дальше немодифицированным хуже,
        чем задвоить публикацию, но лучше, чем потерять новость. Ошибка
        логируется на ERROR с id поста.
        """
        try:
            survivors = self.check(post, results)
        except Exception:
            self._logger.exception(
                "ошибка дедупликации, пост передан дальше без проверки на дубль: "
                "source=%s message_id=%s",
                post.source_id,
                post.message_id,
            )
            survivors = results

        if survivors:

            def _commit(survivors: list[MatchResult] = survivors) -> None:
                self.commit(post, survivors)

            await self._sink.handle(post, _commit)

    def check(self, post: Post, results: list[MatchResult]) -> list[MatchResult]:
        """Проверка на дубль — §6 v2.3: только читает буфер, ничего не пишет.

        Возвращает результаты, не признанные дублем. Вызывающий код решает,
        когда (и решает ли вообще) звать `commit` с этим же списком.
        """
        bundle = self._config_store.get()
        window = dt.timedelta(hours=bundle.config.runtime.dedup_window_hours)
        threshold = bundle.config.runtime.dedup_threshold

        self._prune(self._now(), window)

        survivors: list[MatchResult] = []
        for result in results:
            duplicate = self._find_duplicate(result.topic_id, result.vector, threshold)
            if duplicate is not None:
                matched_entry, similarity = duplicate
                # §6, пункт 5 промпта: без id поста, с которым совпало, и
                # значения сходства разбирать ложные срабатывания будет не
                # по чему.
                self._logger.info(
                    "дубль отброшен: тема=%s source=%s message_id=%s сходство=%.4f "
                    "совпадает_с_source=%s совпадает_с_message_id=%s",
                    result.topic_id,
                    post.source_id,
                    post.message_id,
                    similarity,
                    matched_entry.source_id,
                    matched_entry.message_id,
                )
                continue
            survivors.append(result)
        return survivors

    def commit(self, post: Post, results: list[MatchResult]) -> None:
        """Фиксация — §6 v2.3: добавить векторы `results` в буфер и сохранить.

        Вызывается только после подтверждённой публикации, не в момент
        прохождения дедупа — иначе пост, прошедший `check`, но не
        опубликованный, заглушит следующий настоящий дубль той же новости.
        """
        if not results:
            return

        now = self._now()
        new_entries = [
            DedupEntry(
                topic_id=result.topic_id,
                source_id=post.source_id,
                message_id=post.message_id,
                vector=result.vector.tolist(),
                ts=now,
            )
            for result in results
        ]

        # §8 v2.2, пункт 2 промпта: файл переписывается только когда буфер
        # реально меняется составом — то есть здесь, при фиксации.
        self._buffer.entries.extend(new_entries)
        self._buffer_store.save(self._buffer)

    def _find_duplicate(
        self, topic_id: str, vector: Vector, threshold: float
    ) -> tuple[DedupEntry, float] | None:
        # §6: дедуп — в пределах одной темы, буфер других тем не участвует.
        best: tuple[DedupEntry, float] | None = None
        for entry in self._buffer.entries:
            if entry.topic_id != topic_id:
                continue
            similarity = _cosine(np.asarray(entry.vector, dtype=np.float32), vector)
            if similarity > threshold and (best is None or similarity > best[1]):
                best = (entry, similarity)
        return best

    def _prune(self, now: dt.datetime, window: dt.timedelta) -> None:
        cutoff = now - window
        self._buffer.entries = [entry for entry in self._buffer.entries if entry.ts >= cutoff]
