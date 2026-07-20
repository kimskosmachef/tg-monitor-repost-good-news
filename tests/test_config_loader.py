from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import MINIMAL_CONFIG, MINIMAL_SOURCES, MINIMAL_TOPICS
from tg_monitor.config_loader import load_config, load_sources, load_topics
from tg_monitor.errors import ConfigParseError, ConfigValidationError


def test_load_config_valid(tmp_path: Path) -> None:
    import yaml

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(MINIMAL_CONFIG), encoding="utf-8")

    config = load_config(path)

    assert config.service_chat == "@tg_monitor_service"
    assert config.runtime.dedup_threshold == 0.85


def test_load_config_missing_field_reports_file_and_path(tmp_path: Path) -> None:
    import yaml

    broken = dict(MINIMAL_CONFIG)
    del broken["service_chat"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(broken), encoding="utf-8")

    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(path)

    message = str(exc_info.value)
    assert str(path) in message
    assert "service_chat" in message


def test_load_config_wrong_type_reports_nested_path(tmp_path: Path) -> None:
    import yaml

    broken = {**MINIMAL_CONFIG, "runtime": {**MINIMAL_CONFIG["runtime"], "dedup_threshold": "n/a"}}  # type: ignore[dict-item]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(broken), encoding="utf-8")

    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(path)

    message = str(exc_info.value)
    assert str(path) in message
    assert "runtime.dedup_threshold" in message


def test_load_config_invalid_yaml_syntax(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("account: [unclosed", encoding="utf-8")

    with pytest.raises(ConfigParseError) as exc_info:
        load_config(path)

    assert str(path) in str(exc_info.value)


def test_load_topics_valid(tmp_path: Path) -> None:
    import yaml

    path = tmp_path / "topics.yaml"
    path.write_text(yaml.safe_dump(MINIMAL_TOPICS), encoding="utf-8")

    topics = load_topics(path)

    assert len(topics) == 1
    assert topics[0].id == "topic_one"
    assert topics[0].facets[0].examples == ["пример поста один", "пример поста два"]


def test_load_topics_empty_facet_examples_rejected(tmp_path: Path) -> None:
    import yaml

    broken = [{**MINIMAL_TOPICS[0], "facets": [{"id": "facet_a", "examples": []}]}]
    path = tmp_path / "topics.yaml"
    path.write_text(yaml.safe_dump(broken), encoding="utf-8")

    with pytest.raises(ConfigValidationError) as exc_info:
        load_topics(path)

    message = str(exc_info.value)
    assert str(path) in message
    assert "facets" in message


def test_load_sources_valid(tmp_path: Path) -> None:
    import yaml

    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump(MINIMAL_SOURCES), encoding="utf-8")

    sources = load_sources(path)

    assert len(sources) == 1
    assert sources[0].ref == "@channel_a"


def test_load_sources_boost_out_of_range_rejected(tmp_path: Path) -> None:
    import yaml

    broken = [{**MINIMAL_SOURCES[0], "boost": 0.5}]
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump(broken), encoding="utf-8")

    with pytest.raises(ConfigValidationError) as exc_info:
        load_sources(path)

    message = str(exc_info.value)
    assert str(path) in message
    assert "boost" in message


def test_example_configs_in_repo_are_valid() -> None:
    repo_config_dir = Path(__file__).resolve().parent.parent / "config"

    config = load_config(repo_config_dir / "config.example.yaml")
    topics = load_topics(repo_config_dir / "topics.example.yaml")
    sources = load_sources(repo_config_dir / "sources.example.yaml")

    assert config.service_chat
    assert len(topics) >= 1
    assert len(sources) >= 1
