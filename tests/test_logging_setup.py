from __future__ import annotations

import logging
from pathlib import Path

from tg_monitor.logging_setup import setup_logging


def test_setup_logging_creates_file_and_writes_record(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "tg_monitor.log"
    setup_logging("INFO", str(log_file))

    logging.getLogger("tg_monitor.test").info("привет")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "привет" in content
    assert "INFO" in content


def test_setup_logging_respects_level(tmp_path: Path) -> None:
    log_file = tmp_path / "tg_monitor.log"
    setup_logging("WARNING", str(log_file))

    logging.getLogger("tg_monitor.test").info("не должно попасть в лог")
    logging.getLogger("tg_monitor.test").warning("должно попасть в лог")
    for handler in logging.getLogger().handlers:
        handler.flush()

    content = log_file.read_text(encoding="utf-8")
    assert "не должно попасть в лог" not in content
    assert "должно попасть в лог" in content
