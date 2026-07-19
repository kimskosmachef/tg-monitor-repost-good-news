"""Логирование: файл + stdout, метки времени Europe/Riga, уровень из конфига."""

from __future__ import annotations

import datetime as dt
import logging
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

RIGA_TZ = ZoneInfo("Europe/Riga")

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class RigaFormatter(logging.Formatter):
    """Formatter с таймстампами в Europe/Riga независимо от системной TZ."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        moment = dt.datetime.fromtimestamp(record.created, tz=RIGA_TZ)
        if datefmt:
            return moment.strftime(datefmt)
        return moment.isoformat(timespec="seconds")


def setup_logging(level: str, log_file: str) -> None:
    """Настроить root-логгер: вывод в stdout и в файл, единый уровень и формат."""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = RigaFormatter(_LOG_FORMAT)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
