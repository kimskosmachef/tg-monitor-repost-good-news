"""Matcher — отбор поста по темам, §3, §5 docs/spec.md.

Считает сходство поста с центроидами граней каждой темы и возвращает список
сработавших тем со score (§3). Между Reader и sink (§9: "Не прошедший отбор
пост логируется с причиной и score, а не исчезает молча") встаёт
`MatchingSink` — она же граница пакета: дальше поста (публикация,
дедупликация) в этом пакете нет.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from tg_monitor.chunking import chunk_text
from tg_monitor.config_store import ConfigStore
from tg_monitor.embedder import Embedder, Vector
from tg_monitor.models import Post, Source, Topic
from tg_monitor.state import compute_topic_centroid_version

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MatchResult:
    """Результат отбора поста по одной теме — §5, пункт 8 промпта пакета 3."""

    topic_id: str
    facet_id: str
    raw_score: float
    final_score: float
    centroid_version: str


@dataclass(frozen=True)
class TopicCentroids:
    """Центроиды граней темы (и опционально — негативных примеров) — §5.1, §5.4."""

    version: str
    facets: dict[str, Vector]
    negative: Vector | None


def _normalized_mean(vectors: Sequence[Vector]) -> Vector:
    # §5.1: среднее нормализованных векторов примеров, результат нормализуется.
    # Embedder уже отдаёт нормализованные векторы (embed()), здесь усредняем
    # и нормализуем сам результат.
    stacked = np.stack(vectors)
    mean = stacked.mean(axis=0)
    norm = float(np.linalg.norm(mean))
    result = mean / norm if norm > 0 else mean
    return np.asarray(result, dtype=np.float32)


def _cosine(a: Vector, b: Vector) -> float:
    # Векторы нормализованы (unit-length) — косинус сходства это скалярное
    # произведение.
    return float(np.dot(a, b))


class CentroidStore:
    """Кэш центроидов тем по хэшу примеров — §5.1, пункт 4 промпта пакета 3.

    Хэш — тот же `compute_topic_centroid_version`, что пишется в state.json
    (§8): по нему видно и в кэше, и в логе публикации, каким набором
    примеров отобран конкретный пост. Пересчёт — на лету при первом
    обращении после смены хэша, без перезапуска процесса: `ConfigStore` сам
    перечитывает topics.yaml по mtime, а `get()` здесь просто сверяет версию.
    """

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder
        self._cache: dict[str, TopicCentroids] = {}

    def get(self, topic: Topic) -> TopicCentroids:
        version = compute_topic_centroid_version(topic)
        cached = self._cache.get(topic.id)
        if cached is not None and cached.version == version:
            return cached
        computed = self._compute(topic, version)
        self._cache[topic.id] = computed
        return computed

    def _compute(self, topic: Topic, version: str) -> TopicCentroids:
        facets = {
            facet.id: _normalized_mean(self._embedder.embed(facet.examples))
            for facet in topic.facets
        }
        negative = (
            _normalized_mean(self._embedder.embed(topic.negatives)) if topic.negatives else None
        )
        logger.info(
            "центроиды темы %s пересчитаны: версия=%s граней=%d негативы=%s",
            topic.id,
            version,
            len(facets),
            bool(negative is not None),
        )
        return TopicCentroids(version=version, facets=facets, negative=negative)


def _topic_applies(topic: Topic, source_id: str) -> bool:
    return topic.sources == "all" or source_id in topic.sources


def source_boost(sources: list[Source], source_id: str) -> float:
    """Надбавка источника к score — §5.6. Источник не найден в реестре → 0."""
    for source in sources:
        if source.id == source_id:
            return source.boost
    return 0.0


def facet_scores(centroids: TopicCentroids, chunk_vectors: list[Vector]) -> dict[str, float]:
    """Сходство поста с каждой гранью темы — максимум по чанкам (§5.1), без вычета негатива.

    Публичная функция: переиспользуется `scripts/score.py` для таблицы
    калибровки по граням, не только победившей.
    """
    return {
        facet_id: max(_cosine(v, centroid) for v in chunk_vectors)
        for facet_id, centroid in centroids.facets.items()
    }


def negative_score(centroids: TopicCentroids, chunk_vectors: list[Vector]) -> float:
    """Максимум сходства поста с негативным центроидом темы — §5.4. Без негативов — 0."""
    if centroids.negative is None:
        return 0.0
    return max(_cosine(v, centroids.negative) for v in chunk_vectors)


def _best_facet(centroids: TopicCentroids, chunk_vectors: list[Vector]) -> tuple[str, float]:
    # §5.1: score темы = максимум по граням. §5.4: если у темы есть
    # негативные примеры, из сходства с позитивным центроидом каждой грани
    # вычитается общий для темы sim с негативным центроидом — max по граням
    # берётся уже по скорректированным значениям.
    sim_negative = negative_score(centroids, chunk_vectors)
    positive = facet_scores(centroids, chunk_vectors)
    adjusted = (
        {facet_id: score - sim_negative for facet_id, score in positive.items()}
        if centroids.negative is not None
        else positive
    )
    return max(adjusted.items(), key=lambda kv: kv[1])


class Matcher:
    """Отбор поста по темам из topics.yaml — §5 docs/spec.md."""

    def __init__(
        self,
        embedder: Embedder,
        config_store: ConfigStore,
        centroid_store: CentroidStore | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self._embedder = embedder
        self._config_store = config_store
        self._centroids = centroid_store or CentroidStore(embedder)
        self._logger = log or logger

    def score_post(self, post: Post) -> list[MatchResult]:
        """Оценить пост по всем темам. §5.3: пост без текста не оценивается вовсе."""
        if not post.text:
            return []
        bundle = self._config_store.get()
        chunks = chunk_text(
            post.text,
            min_chunk_chars=bundle.config.embedder.min_chunk_chars,
            max_chunk_chars=bundle.config.embedder.max_chunk_chars,
        )
        if not chunks:
            return []
        chunk_vectors = self._embedder.embed(chunks)
        boost = source_boost(bundle.sources, post.source_id)

        results: list[MatchResult] = []
        for topic in bundle.topics:
            if not _topic_applies(topic, post.source_id):
                continue
            result = self._score_topic(topic, chunk_vectors, boost, post)
            if result is not None:
                results.append(result)
        return results

    def _score_topic(
        self,
        topic: Topic,
        chunk_vectors: list[Vector],
        boost: float,
        post: Post,
    ) -> MatchResult | None:
        centroids = self._centroids.get(topic)
        facet_id, raw_score = _best_facet(centroids, chunk_vectors)
        # §5.6: boost надбавляется к score, но не подменяет сырой score в логах
        # — иначе калибровка порога поедет.
        final_score = raw_score + boost

        if topic.threshold is None:
            # §5.5: shadow-режим — порог ещё не откалиброван, boost в решении
            # не участвует (§5.6: "boost не применяется при калибровке
            # порога"), отсекается только явный шум по мягкому порогу.
            passed = raw_score > topic.soft_threshold
            mode = "shadow"
        else:
            passed = final_score > topic.threshold
            mode = "boxed"

        self._logger.debug(
            "тема=%s грань=%s режим=%s raw=%.4f final=%.4f boost=%.4f "
            "порог=%s мягкий_порог=%.4f прошёл=%s source=%s message_id=%s",
            topic.id,
            facet_id,
            mode,
            raw_score,
            final_score,
            boost,
            topic.threshold,
            topic.soft_threshold,
            passed,
            post.source_id,
            post.message_id,
        )
        if not passed:
            return None
        return MatchResult(
            topic_id=topic.id,
            facet_id=facet_id,
            raw_score=raw_score,
            final_score=final_score,
            centroid_version=centroids.version,
        )


class Sink(Protocol):
    """Приёмник постов ниже по цепочке — тот же структурный протокол, что `reader.Sink`."""

    async def handle(self, post: Post) -> None: ...


class MatchingSink:
    """Встаёт между Reader и sink — §3, §5, пункт 9 промпта пакета 3.

    Publisher (пакет 5) ещё не существует, поэтому пост, прошедший хотя бы
    одну тему, просто передаётся дальше как есть — какой sink стоит ниже
    (сейчас `LoggingSink`), решает вызывающий код. Здесь фиксируется только
    решение "прошёл/не прошёл" и его причина в логе (CLAUDE.md: молчаливых
    потерь постов быть не должно).
    """

    def __init__(
        self,
        matcher: Matcher,
        sink: Sink,
        log: logging.Logger | None = None,
    ) -> None:
        self._matcher = matcher
        self._sink = sink
        self._logger = log or logger

    async def handle(self, post: Post) -> None:
        if not post.text:
            self._logger.info(
                "пост без текста не оценивается: source=%s message_id=%s",
                post.source_id,
                post.message_id,
            )
            return

        results = self._matcher.score_post(post)
        if not results:
            self._logger.info(
                "пост не прошёл ни одной темы: source=%s message_id=%s",
                post.source_id,
                post.message_id,
            )
            return

        for result in results:
            self._logger.info(
                "пост прошёл тему=%s грань=%s raw=%.4f final=%.4f центроид=%s "
                "source=%s message_id=%s",
                result.topic_id,
                result.facet_id,
                result.raw_score,
                result.final_score,
                result.centroid_version,
                post.source_id,
                post.message_id,
            )
        await self._sink.handle(post)
