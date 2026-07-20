"""Логирование: файл + stdout, метки времени в таймзоне из конфига (§4)."""

from __future__ import annotations

import datetime as dt
import logging
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class _TZFormatter(logging.Formatter):
    """Formatter с таймстампами в заданной таймзоне независимо от системной TZ."""

    def __init__(self, fmt: str, tz: ZoneInfo) -> None:
        super().__init__(fmt)
        self._tz = tz

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        moment = dt.datetime.fromtimestamp(record.created, tz=self._tz)
        if datefmt:
            return moment.strftime(datefmt)
        return moment.isoformat(timespec="seconds")


def setup_logging(level: str, log_file: str, timezone: str) -> None:
    """Настроить root-логгер: вывод в stdout и в файл, единый уровень, формат и таймзона."""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = _TZFormatter(_LOG_FORMAT, ZoneInfo(timezone))

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
