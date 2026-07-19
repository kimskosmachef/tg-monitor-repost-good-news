"""Хранилище состояния — §8 docs/spec.md.

`state.json`: last_message_id по источникам, буфер векторов дедупа,
версия центроида каждой темы (хэш от примеров). Запись атомарная
(tempfile + os.replace). Отсутствие или порча файла — не падение,
старт с текущего момента (буфер дедупа и last_message_id пустые).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from tg_monitor.models import Topic


class DedupEntry(BaseModel):
    """Один вектор в кольцевом буфере дедупа — §6."""

    topic_id: str
    vector: list[float]
    ts: dt.datetime


class StateData(BaseModel):
    """Полная схема state.json — §8."""

    last_message_id: dict[str, int] = Field(default_factory=dict)
    dedup_buffer: list[DedupEntry] = Field(default_factory=list)
    topic_centroid_versions: dict[str, str] = Field(default_factory=dict)


def compute_topic_centroid_version(topic: Topic) -> str:
    """Хэш от примеров темы — чтобы по логу было видно, каким набором отобран пост (§8)."""
    parts: list[str] = []
    for facet in topic.facets:
        parts.append(f"facet:{facet.id}")
        parts.extend(facet.examples)
    parts.extend(f"negative:{n}" for n in topic.negatives)
    payload = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class StateStore:
    """Загрузка/сохранение state.json с атомарной записью."""

    def __init__(self, path: Path, logger: logging.Logger | None = None) -> None:
        self._path = path
        self._logger = logger or logging.getLogger(__name__)

    def load(self) -> StateData:
        if not self._path.exists():
            self._logger.info("%s не найден, старт с текущего момента", self._path)
            return StateData()
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            self._logger.error(
                "%s повреждён (%s), старт с текущего момента, буфер дедупа пуст",
                self._path,
                exc,
            )
            return StateData()
        try:
            return StateData.model_validate(data)
        except (ValidationError, TypeError) as exc:
            self._logger.error(
                "%s не соответствует схеме (%s), старт с текущего момента", self._path, exc
            )
            return StateData()

    def save(self, state: StateData) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = state.model_dump_json(indent=2)
        fd, tmp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                tmp_file.write(payload)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            Path(tmp_name).replace(self._path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
