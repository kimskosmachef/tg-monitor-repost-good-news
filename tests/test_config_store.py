from __future__ import annotations

import os
import time
from pathlib import Path

import yaml

from tests.conftest import MINIMAL_SOURCES, MINIMAL_TOPICS, write_valid_config_set
from tg_monitor.config_store import ConfigStore


def _touch_later(path: Path) -> None:
    """Гарантировать, что mtime строго больше предыдущего (устойчиво к грубому разрешению ФС)."""
    current = path.stat().st_mtime
    new_mtime = max(current + 1, time.time() + 1)
    os.utime(path, (new_mtime, new_mtime))


def test_get_returns_initial_valid_bundle(tmp_path: Path) -> None:
    write_valid_config_set(tmp_path)
    store = ConfigStore(tmp_path / "config.yaml")

    bundle = store.get()

    assert bundle.config.service_chat == "@tg_monitor_service"
    assert [t.id for t in bundle.topics] == ["topic_one"]
    assert [s.id for s in bundle.sources] == ["src_a"]


def test_topics_hot_reload_on_change(tmp_path: Path) -> None:
    write_valid_config_set(tmp_path)
    store = ConfigStore(tmp_path / "config.yaml")
    assert [t.id for t in store.get().topics] == ["topic_one"]

    updated_topics = [{**MINIMAL_TOPICS[0], "id": "topic_renamed"}]
    topics_path = tmp_path / "topics.yaml"
    topics_path.write_text(yaml.safe_dump(updated_topics), encoding="utf-8")
    _touch_later(topics_path)

    bundle = store.get()

    assert [t.id for t in bundle.topics] == ["topic_renamed"]


def test_broken_topics_file_keeps_previous_valid_version(tmp_path: Path) -> None:
    write_valid_config_set(tmp_path)
    store = ConfigStore(tmp_path / "config.yaml")
    assert [t.id for t in store.get().topics] == ["topic_one"]

    topics_path = tmp_path / "topics.yaml"
    topics_path.write_text("- id: [unclosed", encoding="utf-8")
    _touch_later(topics_path)

    bundle = store.get()

    assert [t.id for t in bundle.topics] == ["topic_one"]


def test_sources_still_reload_when_topics_broken(tmp_path: Path) -> None:
    write_valid_config_set(tmp_path)
    store = ConfigStore(tmp_path / "config.yaml")

    topics_path = tmp_path / "topics.yaml"
    topics_path.write_text("not: [valid", encoding="utf-8")
    _touch_later(topics_path)

    sources_path = tmp_path / "sources.yaml"
    updated_sources = [{**MINIMAL_SOURCES[0], "id": "src_renamed"}]
    sources_path.write_text(yaml.safe_dump(updated_sources), encoding="utf-8")
    _touch_later(sources_path)

    bundle = store.get()

    assert [t.id for t in bundle.topics] == ["topic_one"]
    assert [s.id for s in bundle.sources] == ["src_renamed"]


def test_broken_config_yaml_keeps_previous_valid_version(tmp_path: Path) -> None:
    write_valid_config_set(tmp_path)
    store = ConfigStore(tmp_path / "config.yaml")

    config_path = tmp_path / "config.yaml"
    config_path.write_text("account: [unclosed", encoding="utf-8")
    _touch_later(config_path)

    bundle = store.get()

    assert bundle.config.service_chat == "@tg_monitor_service"


def test_recovers_after_fixing_broken_file(tmp_path: Path) -> None:
    write_valid_config_set(tmp_path)
    store = ConfigStore(tmp_path / "config.yaml")

    topics_path = tmp_path / "topics.yaml"
    topics_path.write_text("not: [valid", encoding="utf-8")
    _touch_later(topics_path)
    assert [t.id for t in store.get().topics] == ["topic_one"]

    fixed_topics = [{**MINIMAL_TOPICS[0], "id": "topic_fixed"}]
    topics_path.write_text(yaml.safe_dump(fixed_topics), encoding="utf-8")
    _touch_later(topics_path)

    bundle = store.get()

    assert [t.id for t in bundle.topics] == ["topic_fixed"]
