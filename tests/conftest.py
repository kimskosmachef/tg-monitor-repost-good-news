from __future__ import annotations

from pathlib import Path

import yaml

MINIMAL_CONFIG: dict[str, object] = {
    "account": {"session_path": "~/.tg-monitor/monitor.session"},
    "service_chat": "@tg_monitor_service",
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
}

MINIMAL_TOPICS: list[dict[str, object]] = [
    {
        "id": "topic_one",
        "target": "@target_channel_one",
        "sources": "all",
        "threshold": None,
        "chunk_strategy": "paragraph",
        "facets": [{"id": "facet_a", "examples_file": "examples/facet_a.txt"}],
    }
]

# Содержимое файлов примеров (§4.2), на которые ссылается MINIMAL_TOPICS —
# путь относительно каталога topics.yaml.
MINIMAL_EXAMPLES: dict[str, list[str]] = {
    "examples/facet_a.txt": ["пример поста один", "пример поста два"],
}

MINIMAL_SOURCES: list[dict[str, object]] = [
    {
        "id": "src_a",
        "ref": "@channel_a",
        "status": "active",
        "tags": ["diaspora"],
        "boost": 0.03,
        "added": "2026-07-20",
        "note": "тестовый источник",
    }
]


def write_examples_files(directory: Path, files: dict[str, list[str]]) -> None:
    """Записать файлы примеров/негативов (§4.2), пути — относительно `directory`."""
    for rel_path, entries in files.items():
        path = directory / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n---\n".join(entries), encoding="utf-8")


def write_valid_config_set(directory: Path) -> None:
    (directory / "config.yaml").write_text(yaml.safe_dump(MINIMAL_CONFIG), encoding="utf-8")
    (directory / "topics.yaml").write_text(yaml.safe_dump(MINIMAL_TOPICS), encoding="utf-8")
    (directory / "sources.yaml").write_text(yaml.safe_dump(MINIMAL_SOURCES), encoding="utf-8")
    write_examples_files(directory, MINIMAL_EXAMPLES)
