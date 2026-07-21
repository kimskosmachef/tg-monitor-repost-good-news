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
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from typing import Protocol

import numpy as np

from tg_monitor.config_store import ConfigStore
from tg_monitor.embedder import Vector
from tg_monitor.matcher import MatchResult
from tg_monitor.models import Post
from tg_monitor.state import DedupEntry, StateData, StateStore

logger = logging.getLogger(__name__)


class Sink(Protocol):
    """Приёмник постов, переживших дедуп — тот же структурный протокол, что `reader.Sink`."""

    async def handle(self, post: Post) -> None: ...


def _cosine(a: Vector, b: Vector) -> float:
    # Векторы Embedder'а нормализованы (unit-length) — косинус это скалярное
    # произведение, как и в matcher._cosine.
    return float(np.dot(a, b))


class Deduplicator:
    """Кольцевой буфер векторов опубликованного, раздельный по темам — §6.

    Буфер живёт в `state.json` (`StateData.dedup_buffer`, §8) и переживает
    перезапуск: та же схема, что читает и пишет `TelegramReader` для
    `last_message_id`, — оба компонента делят один и тот же объект `state` и
    один `StateStore`, независимо сохраняя его при своих изменениях.
    """

    def __init__(
        self,
        config_store: ConfigStore,
        state_store: StateStore,
        state: StateData,
        sink: Sink,
        log: logging.Logger | None = None,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self._config_store = config_store
        self._state_store = state_store
        self._state = state
        self._sink = sink
        self._logger = log or logger
        self._now = now

        # §8, пункт 4 промпта: записи старше окна вычищаются уже при загрузке,
        # не только по ходу работы — иначе состояние, накопленное до долгого
        # простоя, раздувало бы буфер до первого же прошедшего поста.
        bundle = self._config_store.get()
        window = dt.timedelta(hours=bundle.config.runtime.dedup_window_hours)
        if self._prune(self._now(), window):
            self._state_store.save(self._state)

    async def handle(self, post: Post, results: list[MatchResult]) -> None:
        """Отфильтровать дубли из `results` и передать пост дальше, если что-то осталось.

        §9, пункт 7 промпта: сбой дедупа не должен ронять обработку и не
        должен отбрасывать пост — пропустить дальше немодифицированным хуже,
        чем задвоить публикацию, но лучше, чем потерять новость. Ошибка
        логируется на ERROR с id поста.
        """
        try:
            survivors = self._filter_duplicates(post, results)
        except Exception:
            self._logger.exception(
                "ошибка дедупликации, пост передан дальше без проверки на дубль: "
                "source=%s message_id=%s",
                post.source_id,
                post.message_id,
            )
            survivors = results

        if survivors:
            await self._sink.handle(post)

    def _filter_duplicates(self, post: Post, results: list[MatchResult]) -> list[MatchResult]:
        bundle = self._config_store.get()
        window = dt.timedelta(hours=bundle.config.runtime.dedup_window_hours)
        threshold = bundle.config.runtime.dedup_threshold
        now = self._now()

        pruned = self._prune(now, window)

        survivors: list[MatchResult] = []
        new_entries: list[DedupEntry] = []
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
            new_entries.append(
                DedupEntry(
                    topic_id=result.topic_id,
                    source_id=post.source_id,
                    message_id=post.message_id,
                    vector=result.vector.tolist(),
                    ts=now,
                )
            )

        if new_entries or pruned:
            self._state.dedup_buffer.extend(new_entries)
            self._state_store.save(self._state)

        return survivors

    def _find_duplicate(
        self, topic_id: str, vector: Vector, threshold: float
    ) -> tuple[DedupEntry, float] | None:
        # §6: дедуп — в пределах одной темы, буфер других тем не участвует.
        best: tuple[DedupEntry, float] | None = None
        for entry in self._state.dedup_buffer:
            if entry.topic_id != topic_id:
                continue
            similarity = _cosine(np.asarray(entry.vector, dtype=np.float32), vector)
            if similarity > threshold and (best is None or similarity > best[1]):
                best = (entry, similarity)
        return best

    def _prune(self, now: dt.datetime, window: dt.timedelta) -> bool:
        cutoff = now - window
        kept = [entry for entry in self._state.dedup_buffer if entry.ts >= cutoff]
        removed = len(kept) != len(self._state.dedup_buffer)
        self._state.dedup_buffer = kept
        return removed
