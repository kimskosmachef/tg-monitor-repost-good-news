from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import pytest

from tg_monitor.models import Facet, Topic
from tg_monitor.state import (
    DedupEntry,
    StateData,
    StateStore,
    compute_topic_centroid_version,
    reconcile_topic_centroid_versions,
)


def test_load_returns_empty_state_when_file_missing(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")

    state = store.load()

    assert state == StateData()
    assert state.last_message_id == {}
    assert state.dedup_buffer == []


def test_load_missing_file_logs_info_not_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # §8: файла никогда не было — законный первый запуск, а не потеря
    # состояния. Уровень ERROR здесь был бы ложной тревогой.
    store = StateStore(tmp_path / "state.json")

    with caplog.at_level(logging.INFO):
        store.load()

    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    assert any(record.levelno == logging.INFO for record in caplog.records)
    assert "первый запуск" in caplog.text


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


# --- §8 v1.9: испорченный файл переименовывается в .bad, не затирается ------


def test_load_corrupt_json_quarantines_file_to_bad(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = StateStore(path)

    store.load()

    assert not path.exists()
    bad_path = tmp_path / "state.json.bad"
    assert bad_path.read_text(encoding="utf-8") == "{not valid json"


def test_load_schema_mismatch_quarantines_file_to_bad(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    original = json.dumps({"last_message_id": "not-a-dict"})
    path.write_text(original, encoding="utf-8")
    store = StateStore(path)

    store.load()

    assert not path.exists()
    bad_path = tmp_path / "state.json.bad"
    assert bad_path.read_text(encoding="utf-8") == original


def test_quarantined_file_is_not_overwritten_by_subsequent_save(tmp_path: Path) -> None:
    # Разбор причины порчи должен оставаться возможным даже после того, как
    # процесс перезаписал state.json валидными данными «с чистого листа».
    path = tmp_path / "state.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = StateStore(path)

    state = store.load()
    store.save(state)

    bad_path = tmp_path / "state.json.bad"
    assert bad_path.read_text(encoding="utf-8") == "{not valid json"
    assert path.exists()  # свежий валидный state.json, отдельно от .bad


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
        facets=[Facet(id="f", examples_file="f.txt", examples=["a", "b"])],
    )
    topic_v2 = Topic(
        id="t",
        target="@t",
        facets=[Facet(id="f", examples_file="f.txt", examples=["a", "b", "c"])],
    )

    assert compute_topic_centroid_version(topic_v1) != compute_topic_centroid_version(topic_v2)


def test_compute_topic_centroid_version_stable_for_same_examples() -> None:
    topic = Topic(
        id="t", target="@t", facets=[Facet(id="f", examples_file="f.txt", examples=["a", "b"])]
    )

    assert compute_topic_centroid_version(topic) == compute_topic_centroid_version(topic)


# --- reconcile_topic_centroid_versions: сверка при старте, §8 v1.8 ----------


def _topic(id_: str, examples: list[str]) -> Topic:
    return Topic(
        id=id_, target="@t", facets=[Facet(id="f", examples_file="f.txt", examples=examples)]
    )


def _state_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    # Конструирование Topic само по себе может варнить (мало примеров в
    # грани) — эти записи приходят от tg_monitor.models, отфильтровываем их,
    # чтобы проверять только то, что логирует сама сверка версий.
    return [record.getMessage() for record in caplog.records if record.name == "tg_monitor.state"]


def test_reconcile_records_version_for_new_topic_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = StateData()
    topic = _topic("t1", ["a"])

    with caplog.at_level(logging.WARNING, logger="tg_monitor.state"):
        reconcile_topic_centroid_versions(state, [topic])

    assert state.topic_centroid_versions["t1"] == compute_topic_centroid_version(topic)
    assert _state_warnings(caplog) == []


def test_reconcile_logs_warning_with_topic_id_when_version_changed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    old_topic = _topic("t1", ["a"])
    state = StateData(topic_centroid_versions={"t1": compute_topic_centroid_version(old_topic)})
    new_topic = _topic("t1", ["a", "b"])

    with caplog.at_level(logging.WARNING, logger="tg_monitor.state"):
        reconcile_topic_centroid_versions(state, [new_topic])

    assert state.topic_centroid_versions["t1"] == compute_topic_centroid_version(new_topic)
    assert any("t1" in message for message in _state_warnings(caplog))


def test_reconcile_does_not_warn_when_version_unchanged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    topic = _topic("t1", ["a"])
    state = StateData(topic_centroid_versions={"t1": compute_topic_centroid_version(topic)})

    with caplog.at_level(logging.WARNING, logger="tg_monitor.state"):
        reconcile_topic_centroid_versions(state, [topic])

    assert _state_warnings(caplog) == []
