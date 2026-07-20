from __future__ import annotations

import logging
from pathlib import Path

import yaml

from tg_monitor.sources_registry import mark_source_unavailable


def _sources() -> list[dict[str, object]]:
    return [
        {
            "id": "src_a",
            "ref": "@a",
            "status": "active",
            "tags": [],
            "boost": 0.0,
            "added": "2026-07-20",
            "note": "",
        },
        {
            "id": "src_b",
            "ref": "@b",
            "status": "active",
            "tags": [],
            "boost": 0.0,
            "added": "2026-07-20",
            "note": "",
        },
    ]


def test_mark_unavailable_only_touches_target_entry(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump(_sources()), encoding="utf-8")

    mark_source_unavailable(path, "src_a", logging.getLogger("test"))

    result = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert result[0]["status"] == "unavailable"
    assert result[1]["status"] == "active"


def test_mark_unavailable_preserves_manual_edit_made_before_the_write(tmp_path: Path) -> None:
    # §4 CLAUDE.md-правки: reader.py читает файл заново непосредственно перед
    # записью (не из кэша ConfigStore), поэтому правка, сделанная руками
    # между чтением ConfigStore и этим вызовом, должна уцелеть.
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump(_sources()), encoding="utf-8")

    on_disk = yaml.safe_load(path.read_text(encoding="utf-8"))
    on_disk[1]["note"] = "правка руками между чтением и записью"
    path.write_text(yaml.safe_dump(on_disk), encoding="utf-8")

    mark_source_unavailable(path, "src_a", logging.getLogger("test"))

    result = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert result[0]["status"] == "unavailable"
    assert result[1]["note"] == "правка руками между чтением и записью"


def test_mark_unavailable_write_is_atomic_no_leftover_tmp_files(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump(_sources()), encoding="utf-8")

    mark_source_unavailable(path, "src_a", logging.getLogger("test"))

    entries = list(tmp_path.iterdir())
    assert entries == [path]


def test_mark_unavailable_write_uses_tempfile_and_replace(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump(_sources()), encoding="utf-8")
    first_inode = path.stat().st_ino

    mark_source_unavailable(path, "src_a", logging.getLogger("test"))

    # os.replace меняет inode: запись идёт через временный файл и
    # переименование, а не правкой существующего файла на месте (§3
    # CLAUDE.md-правок, как в state.py).
    assert path.stat().st_ino != first_inode
