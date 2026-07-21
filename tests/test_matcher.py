from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest
import yaml

from tg_monitor.config_store import ConfigStore
from tg_monitor.embedder import Vector
from tg_monitor.matcher import CentroidStore, Matcher, MatchingSink
from tg_monitor.models import Facet, Post, Topic

FIXED_DATE = dt.datetime(2026, 7, 20, 15, 0, tzinfo=dt.UTC)


# --- фейковый Embedder: детерминированные векторы без сети и модели --------


class DictEmbedder:
    """Отдаёт заранее заданный вектор для точного текста чанка/примера.

    KeyError на неизвестном тексте — намеренно: тест должен явно перечислить
    все тексты, которые дойдут до Embedder (примеры граней/негативов и
    итоговые чанки поста), иначе рассинхрон с чанкованием останется незамеченным.
    """

    def __init__(self, vectors: dict[str, Sequence[float]]) -> None:
        self._vectors = {text: _unit(*values) for text, values in vectors.items()}

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        return [self._vectors[text] for text in texts]


class CountingEmbedder:
    """Оборачивает другой Embedder и считает вызовы embed() — для теста кэша центроидов."""

    def __init__(self, inner: DictEmbedder) -> None:
        self._inner = inner
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        self.calls += 1
        return self._inner.embed(texts)


def _unit(*components: float) -> Vector:
    vector = np.array(components, dtype=np.float32)
    return vector / np.linalg.norm(vector)


# --- построение Topic напрямую (для CentroidStore) --------------------------


def _topic(
    id_: str = "t",
    facets: list[tuple[str, list[str]]] = (("f1", ["пример а"]),),  # type: ignore[assignment]
    negatives: list[str] = (),  # type: ignore[assignment]
    threshold: float | None = 0.0,
    soft_threshold: float = 0.2,
    sources: str | list[str] = "all",
) -> Topic:
    return Topic(
        id=id_,
        target="@target",
        sources=sources,
        threshold=threshold,
        soft_threshold=soft_threshold,
        facets=[Facet(id=fid, examples=examples) for fid, examples in facets],
        negatives=list(negatives),
    )


# --- построение config.yaml/topics.yaml/sources.yaml на диске (для Matcher) -


def _write_configs(
    tmp_path: Path,
    topics: list[dict[str, object]],
    sources: list[dict[str, object]] | None = None,
    min_chunk_chars: int = 1,
    max_chunk_chars: int = 1000,
) -> ConfigStore:
    config = {
        "account": {"session_path": "~/.tg-monitor/monitor.session"},
        "service_chat": "@svc",
        "sources_file": "sources.yaml",
        "topics_file": "topics.yaml",
        "runtime": {
            "catchup_interval_min": 15,
            "dedup_window_hours": 48,
            "dedup_threshold": 0.85,
            "publish_delay_sec": 3,
            "max_post_age_min": 120,
            "rate_limit_per_hour": None,
            "forward_reposts": True,
        },
        "embedder": {
            "model": "unused-in-tests",
            "cache_dir": "~/.tg-monitor/models",
            "device": "cpu",
            "min_chunk_chars": min_chunk_chars,
            "max_chunk_chars": max_chunk_chars,
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (tmp_path / "topics.yaml").write_text(yaml.safe_dump(topics), encoding="utf-8")
    (tmp_path / "sources.yaml").write_text(yaml.safe_dump(sources or []), encoding="utf-8")
    return ConfigStore(tmp_path / "config.yaml")


def _topic_dict(
    id_: str = "t",
    facets: dict[str, list[str]] | None = None,
    negatives: list[str] | None = None,
    threshold: float | None = 0.0,
    soft_threshold: float = 0.2,
    sources: str | list[str] = "all",
) -> dict[str, object]:
    facets = facets or {"f1": ["пример а"]}
    return {
        "id": id_,
        "target": "@target",
        "sources": sources,
        "threshold": threshold,
        "soft_threshold": soft_threshold,
        "chunk_strategy": "paragraph",
        "facets": [{"id": fid, "examples": examples} for fid, examples in facets.items()],
        "negatives": negatives or [],
    }


def _source_dict(id_: str, boost: float = 0.0) -> dict[str, object]:
    return {
        "id": id_,
        "ref": f"@{id_}",
        "status": "active",
        "tags": [],
        "boost": boost,
        "added": "2026-07-20",
        "note": "",
    }


def _post(text: str | None, source_id: str = "src_a", message_id: int = 1) -> Post:
    return Post(
        message_id=message_id,
        source_id=source_id,
        date=FIXED_DATE,
        text=text,
        origin="live",
    )


# --- CentroidStore: кэш по хэшу примеров, пункт 4 ---------------------------


def test_centroid_store_does_not_recompute_for_unchanged_topic() -> None:
    embedder = CountingEmbedder(DictEmbedder({"пример а": (1, 0)}))
    store = CentroidStore(embedder)
    topic = _topic(facets=[("f1", ["пример а"])])

    first = store.get(topic)
    calls_after_first = embedder.calls
    second = store.get(topic)

    assert embedder.calls == calls_after_first
    assert second is first


def test_centroid_store_recomputes_when_examples_change() -> None:
    embedder = CountingEmbedder(DictEmbedder({"пример а": (1, 0), "пример б": (0, 1)}))
    store = CentroidStore(embedder)
    topic_v1 = _topic(id_="t", facets=[("f1", ["пример а"])])
    topic_v2 = _topic(id_="t", facets=[("f1", ["пример б"])])

    first = store.get(topic_v1)
    calls_after_first = embedder.calls
    second = store.get(topic_v2)

    assert embedder.calls > calls_after_first
    assert second.version != first.version


# --- Matcher: максимум по граням, пункт 5 ------------------------------------


def test_matcher_picks_max_scoring_facet(tmp_path: Path) -> None:
    topics = [
        _topic_dict(
            id_="t1",
            facets={"facet_a": ["пример а"], "facet_b": ["пример б"]},
            threshold=0.5,
        )
    ]
    config_store = _write_configs(tmp_path, topics)
    embedder = DictEmbedder({"пример а": (1, 0), "пример б": (0, 1), "текст поста": (0, 1)})
    matcher = Matcher(embedder=embedder, config_store=config_store)

    results = matcher.score_post(_post("текст поста"))

    assert len(results) == 1
    assert results[0].topic_id == "t1"
    assert results[0].facet_id == "facet_b"
    assert results[0].raw_score == pytest.approx(1.0)


# --- Matcher: максимум по чанкам, а не среднее, пункт 5 ----------------------


def test_matcher_takes_max_over_chunks_not_average(tmp_path: Path) -> None:
    topics = [_topic_dict(id_="t1", facets={"facet_a": ["пример а"]}, threshold=0.5)]
    config_store = _write_configs(tmp_path, topics)
    # Абзацы длиной >=1 символ (min_chunk_chars=1) не клеятся — остаются двумя
    # чанками: один противоположен грани (cos=-1), другой совпадает (cos=1).
    embedder = DictEmbedder(
        {"пример а": (1, 0), "чанк не по теме": (-1, 0), "чанк точно по теме": (1, 0)}
    )
    matcher = Matcher(embedder=embedder, config_store=config_store)

    results = matcher.score_post(_post("чанк не по теме\n\nчанк точно по теме"))

    assert len(results) == 1
    assert results[0].raw_score == pytest.approx(1.0)


# --- Matcher: негативные примеры, §5.4, пункт 5 ------------------------------


def test_matcher_negative_example_suppresses_false_positive(tmp_path: Path) -> None:
    topics = [
        _topic_dict(
            id_="t1",
            facets={"facet_a": ["позитивный пример"]},
            negatives=["негативный пример"],
            threshold=0.3,
        )
    ]
    config_store = _write_configs(tmp_path, topics)
    # Чанк равноудалён от позитивного и негативного центроида: sim_positive =
    # sim_negative = 1/sqrt(2) ≈ 0.707 — сам по себе высокий сырой score, но
    # sim_positive - sim_negative = 0, порог 0.3 не проходится.
    embedder = DictEmbedder(
        {
            "позитивный пример": (1, 0),
            "негативный пример": (0, 1),
            "текст поста": (1, 1),
        }
    )
    matcher = Matcher(embedder=embedder, config_store=config_store)

    results = matcher.score_post(_post("текст поста"))

    assert results == []


def test_matcher_passes_when_far_from_negative(tmp_path: Path) -> None:
    topics = [
        _topic_dict(
            id_="t1",
            facets={"facet_a": ["позитивный пример"]},
            negatives=["негативный пример"],
            threshold=0.3,
        )
    ]
    config_store = _write_configs(tmp_path, topics)
    embedder = DictEmbedder(
        {
            "позитивный пример": (1, 0),
            "негативный пример": (0, 1),
            "текст поста": (1, 0),
        }
    )
    matcher = Matcher(embedder=embedder, config_store=config_store)

    results = matcher.score_post(_post("текст поста"))

    assert len(results) == 1
    assert results[0].raw_score == pytest.approx(1.0)


# --- Matcher: boost источника, §5.6, пункт 6 ---------------------------------


def test_matcher_applies_boost_and_logs_raw_separately(tmp_path: Path) -> None:
    topics = [_topic_dict(id_="t1", facets={"facet_a": ["пример а"]}, threshold=0.5)]
    sources = [_source_dict("src_a", boost=0.05)]
    config_store = _write_configs(tmp_path, topics, sources)
    embedder = DictEmbedder({"пример а": (1, 0), "текст поста": (1, 0)})
    matcher = Matcher(embedder=embedder, config_store=config_store)

    results = matcher.score_post(_post("текст поста", source_id="src_a"))

    assert len(results) == 1
    result = results[0]
    assert result.raw_score == pytest.approx(1.0)
    assert result.final_score == pytest.approx(result.raw_score + 0.05)


def test_matcher_boxed_mode_decides_on_final_score(tmp_path: Path) -> None:
    # threshold=1.02 недостижим сырым score (максимум 1.0 при идеальном
    # совпадении), но final_score = raw + boost его превышает.
    topics = [_topic_dict(id_="t1", facets={"facet_a": ["пример а"]}, threshold=1.02)]
    sources = [_source_dict("src_a", boost=0.05)]
    config_store = _write_configs(tmp_path, topics, sources)
    embedder = DictEmbedder({"пример а": (1, 0), "текст поста": (1, 0)})
    matcher = Matcher(embedder=embedder, config_store=config_store)

    results = matcher.score_post(_post("текст поста", source_id="src_a"))

    assert len(results) == 1
    assert results[0].final_score > 1.02


# --- Matcher: shadow-режим (threshold: null), §5.5, пункт 7 ------------------


def test_matcher_shadow_mode_uses_soft_threshold(tmp_path: Path) -> None:
    topics = [
        _topic_dict(id_="t1", facets={"facet_a": ["пример а"]}, threshold=None, soft_threshold=0.5)
    ]
    config_store = _write_configs(tmp_path, topics)
    # cos(45°) ≈ 0.707 > soft_threshold=0.5.
    embedder = DictEmbedder({"пример а": (1, 0), "текст поста": (1, 1)})
    matcher = Matcher(embedder=embedder, config_store=config_store)

    results = matcher.score_post(_post("текст поста"))

    assert len(results) == 1
    assert results[0].raw_score == pytest.approx(0.7071, abs=1e-3)


def test_matcher_shadow_mode_ignores_boost_in_decision(tmp_path: Path) -> None:
    # raw_score не проходит soft_threshold, но boost достаточно большой,
    # чтобы final_score его превысил — §5.6: "boost не применяется при
    # калибровке порога". Пост не должен пройти.
    topics = [
        _topic_dict(id_="t1", facets={"facet_a": ["пример а"]}, threshold=None, soft_threshold=0.9)
    ]
    sources = [_source_dict("src_a", boost=0.5)]
    config_store = _write_configs(tmp_path, topics, sources)
    embedder = DictEmbedder({"пример а": (1, 0), "текст поста": (1, 1)})
    matcher = Matcher(embedder=embedder, config_store=config_store)

    results = matcher.score_post(_post("текст поста", source_id="src_a"))

    assert results == []


# --- Matcher: пост без текста не оценивается, §5.3, пункт 5 ------------------


def test_matcher_skips_post_without_text(tmp_path: Path) -> None:
    topics = [_topic_dict(id_="t1", threshold=0.0)]
    config_store = _write_configs(tmp_path, topics)
    embedder = DictEmbedder({"пример а": (1, 0)})
    matcher = Matcher(embedder=embedder, config_store=config_store)

    assert matcher.score_post(_post(None)) == []


# --- Matcher: область действия темы по sources -------------------------------


def test_matcher_skips_topic_when_source_not_in_scope(tmp_path: Path) -> None:
    topics = [
        _topic_dict(id_="t1", facets={"facet_a": ["пример а"]}, threshold=0.0, sources=["src_b"])
    ]
    config_store = _write_configs(tmp_path, topics)
    embedder = DictEmbedder({"пример а": (1, 0), "текст поста": (1, 0)})
    matcher = Matcher(embedder=embedder, config_store=config_store)

    results = matcher.score_post(_post("текст поста", source_id="src_a"))

    assert results == []


def test_matcher_one_post_can_pass_several_topics(tmp_path: Path) -> None:
    topics = [
        _topic_dict(id_="t1", facets={"facet_a": ["пример а"]}, threshold=0.5),
        _topic_dict(id_="t2", facets={"facet_a": ["пример а"]}, threshold=0.5),
    ]
    config_store = _write_configs(tmp_path, topics)
    embedder = DictEmbedder({"пример а": (1, 0), "текст поста": (1, 0)})
    matcher = Matcher(embedder=embedder, config_store=config_store)

    results = matcher.score_post(_post("текст поста"))

    assert {r.topic_id for r in results} == {"t1", "t2"}


# --- MatchingSink: не теряет посты молча, пункт 9 ----------------------------


class RecordingSink:
    def __init__(self) -> None:
        self.posts: list[Post] = []

    async def handle(self, post: Post) -> None:
        self.posts.append(post)


def test_matching_sink_forwards_matched_post_once(tmp_path: Path) -> None:
    topics = [
        _topic_dict(id_="t1", facets={"facet_a": ["пример а"]}, threshold=0.5),
        _topic_dict(id_="t2", facets={"facet_a": ["пример а"]}, threshold=0.5),
    ]
    config_store = _write_configs(tmp_path, topics)
    embedder = DictEmbedder({"пример а": (1, 0), "текст поста": (1, 0)})
    matcher = Matcher(embedder=embedder, config_store=config_store)
    downstream = RecordingSink()
    sink = MatchingSink(matcher=matcher, sink=downstream)

    asyncio.run(sink.handle(_post("текст поста")))

    assert len(downstream.posts) == 1


def test_matching_sink_does_not_forward_unmatched_post(tmp_path: Path) -> None:
    topics = [_topic_dict(id_="t1", facets={"facet_a": ["пример а"]}, threshold=0.99)]
    config_store = _write_configs(tmp_path, topics)
    embedder = DictEmbedder({"пример а": (1, 0), "текст поста": (0, 1)})
    matcher = Matcher(embedder=embedder, config_store=config_store)
    downstream = RecordingSink()
    sink = MatchingSink(matcher=matcher, sink=downstream)

    asyncio.run(sink.handle(_post("текст поста")))

    assert downstream.posts == []


def test_matching_sink_does_not_forward_post_without_text(tmp_path: Path) -> None:
    topics = [_topic_dict(id_="t1")]
    config_store = _write_configs(tmp_path, topics)
    embedder = DictEmbedder({"пример а": (1, 0)})
    matcher = Matcher(embedder=embedder, config_store=config_store)
    downstream = RecordingSink()
    sink = MatchingSink(matcher=matcher, sink=downstream)

    asyncio.run(sink.handle(_post(None)))

    assert downstream.posts == []
