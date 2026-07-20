from __future__ import annotations

import logging
from pathlib import Path

from tg_monitor.logging_setup import setup_logging


def test_setup_logging_creates_file_and_writes_record(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "tg-monitor.log"
    setup_logging("INFO", str(log_file), "Europe/Riga")

    logging.getLogger("tg_monitor.test").info("привет")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "привет" in content
    assert "INFO" in content


def test_setup_logging_respects_level(tmp_path: Path) -> None:
    log_file = tmp_path / "tg-monitor.log"
    setup_logging("WARNING", str(log_file), "Europe/Riga")

    logging.getLogger("tg_monitor.test").info("не должно попасть в лог")
    logging.getLogger("tg_monitor.test").warning("должно попасть в лог")
    for handler in logging.getLogger().handlers:
        handler.flush()

    content = log_file.read_text(encoding="utf-8")
    assert "не должно попасть в лог" not in content
    assert "должно попасть в лог" in content


def _log_and_get_offset(log_file: Path, timezone: str, marker: str) -> str:
    setup_logging("INFO", str(log_file), timezone)
    logging.getLogger("tg_monitor.test").info(marker)
    for handler in logging.getLogger().handlers:
        handler.flush()
    lines = log_file.read_text(encoding="utf-8").splitlines()
    line = next(line for line in lines if marker in line)
    return line.split()[0][-6:]


def test_setup_logging_uses_configured_timezone(tmp_path: Path) -> None:
    riga_offset = _log_and_get_offset(tmp_path / "riga.log", "Europe/Riga", "метка риги")
    tokyo_offset = _log_and_get_offset(tmp_path / "tokyo.log", "Asia/Tokyo", "метка токио")

    assert riga_offset != tokyo_offset
