from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
import yaml

from tg_monitor.config_store import ConfigStore
from tg_monitor.deduplicator import Deduplicator
from tg_monitor.embedder import Vector
from tg_monitor.matcher import MatchResult
from tg_monitor.models import Post
from tg_monitor.state import DedupBufferData, DedupBufferStore, DedupEntry

FIXED_DATE = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.UTC)


def _unit(*components: float) -> Vector:
    vector = np.array(components, dtype=np.float32)
    return vector / np.linalg.norm(vector)


def _result(topic_id: str, vector: Vector, facet_id: str = "f1") -> MatchResult:
    return MatchResult(
        topic_id=topic_id,
        facet_id=facet_id,
        raw_score=0.9,
        final_score=0.9,
        centroid_version="v1",
        vector=vector,
    )


def _post(message_id: int, source_id: str = "src_a") -> Post:
    return Post(
        message_id=message_id, source_id=source_id, date=FIXED_DATE, text="текст", origin="live"
    )


def _config_store(tmp_path: Path, window_hours: int = 48, threshold: float = 0.85) -> ConfigStore:
    # Deduplicator не читает topics.yaml/sources.yaml — минимальные пустые
    # списки, всё внимание на runtime.dedup_window_hours/dedup_threshold.
    config = {
        "account": {"session_path": "~/.tg-monitor/monitor.session"},
        "service_chat": "@svc",
        "sources_file": "sources.yaml",
        "topics_file": "topics.yaml",
        "runtime": {
            "catchup_interval_min": 15,
            "dedup_window_hours": window_hours,
            "dedup_threshold": threshold,
            "publish_delay_sec": 3,
            "max_post_age_min": 120,
            "rate_limit_per_hour": None,
            "forward_reposts": True,
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (tmp_path / "topics.yaml").write_text("[]", encoding="utf-8")
    (tmp_path / "sources.yaml").write_text("[]", encoding="utf-8")
    return ConfigStore(tmp_path / "config.yaml")


class RecordingSink:
    """Приёмник для тестов — подтверждает публикацию безусловно, как и
    временная заглушка на месте будущего Publisher (§6 v2.3)."""

    def __init__(self) -> None:
        self.posts: list[Post] = []

    async def handle(self, post: Post, commit: Callable[[], None]) -> None:
        self.posts.append(post)
        commit()


class NonCommittingSink:
    """Приёмник, который получает пост, но не подтверждает публикацию —
    имитирует запрет пересылки / ошибку отправки / снятие по лимиту (§6
    v2.3): вектор не должен попасть в буфер дедупа."""

    def __init__(self) -> None:
        self.posts: list[Post] = []

    async def handle(self, post: Post, commit: Callable[[], None]) -> None:
        self.posts.append(post)


def _clock(*values: dt.datetime) -> Callable[[], dt.datetime]:
    """Фиксированные "текущие" моменты времени, по одному на вызов `now()`."""
    iterator = iter(values)

    def _now() -> dt.datetime:
        try:
            return next(iterator)
        except StopIteration:
            return values[-1]

    return _now


def _make_dedup(
    tmp_path: Path,
    buffer: DedupBufferData | None = None,
    window_hours: int = 48,
    threshold: float = 0.85,
    now: Callable[[], dt.datetime] | None = None,
    sink: RecordingSink | None = None,
) -> tuple[Deduplicator, ConfigStore, DedupBufferStore, RecordingSink]:
    config_store = _config_store(tmp_path, window_hours=window_hours, threshold=threshold)
    buffer_store = DedupBufferStore(tmp_path / "dedup-buffer.json")
    resolved_buffer = buffer if buffer is not None else DedupBufferData()
    resolved_sink = sink or RecordingSink()
    dedup = Deduplicator(
        config_store=config_store,
        buffer_store=buffer_store,
        buffer=resolved_buffer,
        sink=resolved_sink,
        now=now or _clock(FIXED_DATE),
    )
    return dedup, config_store, buffer_store, resolved_sink


# --- попадание в окно: дубль глушится, §6, пункт 1 промпта -------------------


def test_similar_post_within_window_is_dropped(tmp_path: Path) -> None:
    dedup, _config_store, _buffer_store, sink = _make_dedup(
        tmp_path, now=_clock(FIXED_DATE, FIXED_DATE, FIXED_DATE + dt.timedelta(minutes=5))
    )

    asyncio.run(dedup.handle(_post(1, "src_a"), [_result("t1", _unit(1, 0))]))
    # Похожий, но не идентичный вектор — тот же порядок величины, что и
    # реальный повтор новости из другого источника.
    asyncio.run(dedup.handle(_post(2, "src_b"), [_result("t1", _unit(0.99, 0.14))]))

    assert [p.message_id for p in sink.posts] == [1]


def test_dissimilar_post_is_not_dropped(tmp_path: Path) -> None:
    dedup, _config_store, _buffer_store, sink = _make_dedup(tmp_path)

    asyncio.run(dedup.handle(_post(1, "src_a"), [_result("t1", _unit(1, 0))]))
    asyncio.run(dedup.handle(_post(2, "src_b"), [_result("t1", _unit(0, 1))]))

    assert [p.message_id for p in sink.posts] == [1, 2]


def test_dropped_duplicate_is_logged_with_similarity_and_matched_post(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    dedup, _config_store, _buffer_store, _sink = _make_dedup(tmp_path)

    with caplog.at_level(logging.INFO, logger="tg_monitor.deduplicator"):
        asyncio.run(dedup.handle(_post(1, "src_a"), [_result("t1", _unit(1, 0))]))
        asyncio.run(dedup.handle(_post(2, "src_b"), [_result("t1", _unit(1, 0))]))

    assert any(
        "дубль отброшен" in r.getMessage()
        and "тема=t1" in r.getMessage()
        and "src_b" in r.getMessage()
        and "message_id=2" in r.getMessage()
        and "совпадает_с_source=src_a" in r.getMessage()
        and "совпадает_с_message_id=1" in r.getMessage()
        for r in caplog.records
    )


# --- разделение буферов по темам, §6, пункт 2 промпта ------------------------


def test_buffers_are_separated_by_topic(tmp_path: Path) -> None:
    dedup, _config_store, _buffer_store, sink = _make_dedup(tmp_path)

    asyncio.run(dedup.handle(_post(1, "src_a"), [_result("t1", _unit(1, 0))]))
    # Тот же вектор, другая тема — другая аудитория, глушить нельзя (§6).
    asyncio.run(dedup.handle(_post(2, "src_b"), [_result("t2", _unit(1, 0))]))

    assert [p.message_id for p in sink.posts] == [1, 2]


def test_one_post_can_be_duplicate_in_one_topic_and_new_in_another(tmp_path: Path) -> None:
    dedup, _config_store, _buffer_store, sink = _make_dedup(tmp_path)

    asyncio.run(dedup.handle(_post(1, "src_a"), [_result("t1", _unit(1, 0))]))
    asyncio.run(
        dedup.handle(
            _post(2, "src_b"),
            [_result("t1", _unit(1, 0)), _result("t2", _unit(1, 0))],
        )
    )

    # Пост 2 дублирует t1, но для t2 это первое попадание — пост должен
    # пройти дальше (в t2 он не дубль).
    assert [p.message_id for p in sink.posts] == [1, 2]


# --- вычистка старых записей, §8, пункт 4 промпта -----------------------------


def test_entries_older_than_window_are_pruned_during_operation(tmp_path: Path) -> None:
    dedup, _config_store, _buffer_store, sink = _make_dedup(
        tmp_path,
        window_hours=1,
        # Проверка и фиксация — разные вызовы `now()` (§6 v2.3): первое
        # значение — прунинг при инициализации (буфер пуст, не важно), второе
        # и третье — check()/commit() первого handle() (пост проходит тему в
        # момент FIXED_DATE), четвёртое — check() второго handle() два часа
        # спустя (commit() второго handle() лишний вызов подхватит последнее
        # значение — на исход теста не влияет).
        now=_clock(FIXED_DATE, FIXED_DATE, FIXED_DATE, FIXED_DATE + dt.timedelta(hours=2)),
    )

    asyncio.run(dedup.handle(_post(1, "src_a"), [_result("t1", _unit(1, 0))]))
    # Второй вызов — два часа спустя, окно всего час: первая запись должна
    # быть вычищена, и идентичный вектор больше не считается дублем.
    asyncio.run(dedup.handle(_post(2, "src_b"), [_result("t1", _unit(1, 0))]))

    assert [p.message_id for p in sink.posts] == [1, 2]


def test_stale_entries_are_pruned_on_load(tmp_path: Path) -> None:
    stale = DedupEntry(
        topic_id="t1",
        source_id="src_old",
        message_id=1,
        vector=[1.0, 0.0],
        ts=FIXED_DATE - dt.timedelta(hours=100),
    )
    buffer = DedupBufferData(entries=[stale])

    dedup, _config_store, _buffer_store, _sink = _make_dedup(
        tmp_path, buffer=buffer, window_hours=48, now=_clock(FIXED_DATE)
    )

    assert dedup._buffer.entries == []


# --- переживание перезапуска, §8 ---------------------------------------------


def test_buffer_survives_restart(tmp_path: Path) -> None:
    config_store = _config_store(tmp_path)
    buffer_path = tmp_path / "dedup-buffer.json"

    buffer_store_a = DedupBufferStore(buffer_path)
    buffer_a = buffer_store_a.load()
    sink_a = RecordingSink()
    dedup_a = Deduplicator(
        config_store=config_store,
        buffer_store=buffer_store_a,
        buffer=buffer_a,
        sink=sink_a,
        now=_clock(FIXED_DATE),
    )
    asyncio.run(dedup_a.handle(_post(1, "src_a"), [_result("t1", _unit(1, 0))]))
    assert sink_a.posts

    # Новый процесс: свежий DedupBufferStore и buffer, загруженные с диска.
    buffer_store_b = DedupBufferStore(buffer_path)
    buffer_b = buffer_store_b.load()
    sink_b = RecordingSink()
    dedup_b = Deduplicator(
        config_store=config_store,
        buffer_store=buffer_store_b,
        buffer=buffer_b,
        sink=sink_b,
        now=_clock(FIXED_DATE + dt.timedelta(minutes=10)),
    )
    asyncio.run(dedup_b.handle(_post(2, "src_b"), [_result("t1", _unit(1, 0))]))

    assert sink_b.posts == []  # признан дублем поста 1 из предыдущего запуска


def test_buffer_not_rewritten_when_post_is_screened_out(tmp_path: Path) -> None:
    # §8 v2.2, пункт 2 промпта: файл переписывается только когда пост прошёл
    # тему. Отсеянный (дублирующий) пост не должен трогать файл на диске,
    # даже если прунинг что-то вычистил из буфера в памяти.
    config_store = _config_store(tmp_path, window_hours=1)
    buffer_path = tmp_path / "dedup-buffer.json"
    stale = DedupEntry(
        topic_id="t1",
        source_id="src_old",
        message_id=0,
        vector=[1.0, 0.0],
        ts=FIXED_DATE - dt.timedelta(hours=100),
    )
    buffer_store = DedupBufferStore(buffer_path)
    buffer_store.save(DedupBufferData(entries=[stale]))
    content_before = buffer_path.read_text(encoding="utf-8")

    sink = RecordingSink()
    dedup = Deduplicator(
        config_store=config_store,
        buffer_store=buffer_store,
        buffer=buffer_store.load(),
        sink=sink,
        now=_clock(FIXED_DATE, FIXED_DATE, FIXED_DATE),
    )
    # Прунинг при инициализации вычищает "stale" (окно час, запись старше
    # ста часов) в памяти, но это не должно тронуть файл — прунинг сам по
    # себе не запись.
    assert buffer_path.read_text(encoding="utf-8") == content_before

    # Первый пост проходит тему и должен вызвать запись.
    asyncio.run(dedup.handle(_post(1, "src_a"), [_result("t1", _unit(1, 0))]))
    content_after_pass = buffer_path.read_text(encoding="utf-8")
    assert content_after_pass != content_before

    # Второй пост — точный дубль первого, отсеян: файл не должен переписаться.
    asyncio.run(dedup.handle(_post(2, "src_b"), [_result("t1", _unit(1, 0))]))
    assert buffer_path.read_text(encoding="utf-8") == content_after_pass


# --- поведение при ошибке, §9, пункт 7 промпта -------------------------------


def test_error_forwards_post_unfiltered_and_logs_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config_store = _config_store(tmp_path)
    buffer_store = DedupBufferStore(tmp_path / "dedup-buffer.json")
    # Вектор другой размерности в буфере — имитирует смену модели эмбеддера
    # без очистки старого буфера: сравнение обязано упасть на несовпадении
    # размерностей, а не молча всё отбросить и не уронить обработку поста.
    buffer = DedupBufferData(
        entries=[
            DedupEntry(
                topic_id="t1",
                source_id="src_old",
                message_id=0,
                vector=[1.0, 0.0, 0.0],
                ts=FIXED_DATE,
            )
        ]
    )
    sink = RecordingSink()
    dedup = Deduplicator(
        config_store=config_store,
        buffer_store=buffer_store,
        buffer=buffer,
        sink=sink,
        now=_clock(FIXED_DATE),
    )

    with caplog.at_level(logging.ERROR, logger="tg_monitor.deduplicator"):
        asyncio.run(dedup.handle(_post(1, "src_a"), [_result("t1", _unit(1, 0))]))

    # Хуже потерять новость, чем допустить дубль — сбой дедупа не должен
    # отбрасывать пост.
    assert [p.message_id for p in sink.posts] == [1]
    assert any(
        record.levelno == logging.ERROR
        and "ошибка дедупликации" in record.getMessage()
        and "src_a" in record.getMessage()
        and "message_id=1" in record.getMessage()
        for record in caplog.records
    )


# --- проверка и фиксация разведены, §6 v2.3 -----------------------------------


def test_check_does_not_mutate_buffer(tmp_path: Path) -> None:
    dedup, _config_store, _buffer_store, _sink = _make_dedup(tmp_path)

    survivors = dedup.check(_post(1, "src_a"), [_result("t1", _unit(1, 0))])

    assert [r.topic_id for r in survivors] == ["t1"]
    assert dedup._buffer.entries == []


def test_without_commit_next_identical_post_is_not_duplicate(tmp_path: Path) -> None:
    dedup, _config_store, _buffer_store, _sink = _make_dedup(tmp_path)

    # Проверка прошла, но фиксации не было.
    dedup.check(_post(1, "src_a"), [_result("t1", _unit(1, 0))])

    # Идентичный вектор всё равно не считается дублем — вектор первого поста
    # в буфер не попал.
    survivors = dedup.check(_post(2, "src_b"), [_result("t1", _unit(1, 0))])
    assert [r.topic_id for r in survivors] == ["t1"]


def test_after_commit_next_identical_post_is_duplicate(tmp_path: Path) -> None:
    dedup, _config_store, _buffer_store, _sink = _make_dedup(tmp_path)

    first_post = _post(1, "src_a")
    survivors = dedup.check(first_post, [_result("t1", _unit(1, 0))])
    dedup.commit(first_post, survivors)

    # Теперь идентичный вектор — дубль.
    second_survivors = dedup.check(_post(2, "src_b"), [_result("t1", _unit(1, 0))])
    assert second_survivors == []


def test_sink_that_skips_commit_leaves_next_duplicate_unblocked(tmp_path: Path) -> None:
    # §6 v2.3, обоснование в docs/spec.md: пост прошёл дедуп, но приёмник не
    # подтвердил публикацию (аналог on_forward_forbidden: skip, ошибки
    # отправки, снятия по лимиту) — вектор не должен попасть в буфер, иначе
    # следующий настоящий дубль той же новости заглушится и она не выйдет
    # вообще.
    config_store = _config_store(tmp_path)
    buffer_store = DedupBufferStore(tmp_path / "dedup-buffer.json")
    sink = NonCommittingSink()
    dedup = Deduplicator(
        config_store=config_store,
        buffer_store=buffer_store,
        buffer=DedupBufferData(),
        sink=sink,
        now=_clock(FIXED_DATE),
    )

    asyncio.run(dedup.handle(_post(1, "src_a"), [_result("t1", _unit(1, 0))]))
    asyncio.run(dedup.handle(_post(2, "src_b"), [_result("t1", _unit(1, 0))]))

    assert [p.message_id for p in sink.posts] == [1, 2]
