from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import pytest

from tg_monitor.models import Facet, Topic
from tg_monitor.state import DedupEntry, StateData, StateStore, compute_topic_centroid_version


def test_load_returns_empty_state_when_file_missing(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")

    state = store.load()

    assert state == StateData()
    assert state.last_message_id == {}
    assert state.dedup_buffer == []


def test_load_missing_file_logs_error_with_last_message_id_lost(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = StateStore(tmp_path / "state.json")

    with caplog.at_level(logging.ERROR):
        store.load()

    assert any(record.levelno == logging.ERROR for record in caplog.records)
    assert "last_message_id" in caplog.text


def test_load_returns_empty_state_when_file_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = StateStore(path)

    state = store.load()

    assert state == StateData()


def test_load_corrupt_file_logs_error_with_last_message_id_lost(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = StateStore(path)

    with caplog.at_level(logging.ERROR):
        store.load()

    assert any(record.levelno == logging.ERROR for record in caplog.records)
    assert "last_message_id" in caplog.text


def test_load_returns_empty_state_when_schema_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"last_message_id": "not-a-dict"}), encoding="utf-8")
    store = StateStore(path)

    state = store.load()

    assert state == StateData()


def test_load_schema_mismatch_logs_error_with_last_message_id_lost(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"last_message_id": "not-a-dict"}), encoding="utf-8")
    store = StateStore(path)

    with caplog.at_level(logging.ERROR):
        store.load()

    assert any(record.levelno == logging.ERROR for record in caplog.records)
    assert "last_message_id" in caplog.text


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    state = StateData(
        last_message_id={"src_a": 42},
        dedup_buffer=[
            DedupEntry(
                topic_id="topic_one",
                vector=[0.1, 0.2, 0.3],
                ts=dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.UTC),
            )
        ],
        topic_centroid_versions={"topic_one": "abc123"},
    )

    store.save(state)
    loaded = store.load()

    assert loaded == state


def test_save_is_atomic_no_leftover_tmp_files(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)

    store.save(StateData(last_message_id={"src_a": 1}))

    entries = list(tmp_path.iterdir())
    assert entries == [path]


def test_save_uses_tempfile_and_replace(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.save(StateData(last_message_id={"src_a": 1}))
    first_inode = path.stat().st_ino

    store.save(StateData(last_message_id={"src_a": 2}))

    loaded = store.load()
    assert loaded.last_message_id == {"src_a": 2}
    # os.replace меняет inode: каждая запись идёт через новый temp-файл,
    # а не правкой существующего файла на месте.
    assert path.stat().st_ino != first_inode


def test_compute_topic_centroid_version_changes_with_examples() -> None:
    topic_v1 = Topic(
        id="t",
        target="@t",
        facets=[Facet(id="f", examples=["a", "b"])],
    )
    topic_v2 = Topic(
        id="t",
        target="@t",
        facets=[Facet(id="f", examples=["a", "b", "c"])],
    )

    assert compute_topic_centroid_version(topic_v1) != compute_topic_centroid_version(topic_v2)


def test_compute_topic_centroid_version_stable_for_same_examples() -> None:
    topic = Topic(id="t", target="@t", facets=[Facet(id="f", examples=["a", "b"])])

    assert compute_topic_centroid_version(topic) == compute_topic_centroid_version(topic)
