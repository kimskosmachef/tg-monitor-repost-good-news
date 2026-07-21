"""Хранилище состояния — §8 docs/spec.md.

`state.json`: last_message_id по источникам, буфер векторов дедупа,
версия центроида каждой темы (хэш от примеров). Запись атомарная
(tempfile + os.replace). Отсутствие или порча файла — не падение,
старт с текущего момента (буфер дедупа и last_message_id пустые).
Испорченный файл не затирается валидным при следующей записи — он
переименовывается в `<имя>.bad`, иначе посмертный разбор причины порчи
невозможен.
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


def reconcile_topic_centroid_versions(
    state: StateData, topics: list[Topic], logger: logging.Logger | None = None
) -> None:
    """Сверить версии центроидов из state.json с вычисленными по текущим topics.yaml (§8).

    Вызывается один раз при старте. Расхождение с сохранённой версией
    означает, что грани темы правились между запусками процесса — без этого
    лога сдвиг результатов после правки примеров выглядел бы необъяснимым.
    Новой темы в сохранённом state ещё нет — это не расхождение, а первая
    запись версии. `state.topic_centroid_versions` обновляется на месте,
    сохранение на диск — забота вызывающего кода.
    """
    log = logger or logging.getLogger(__name__)
    for topic in topics:
        version = compute_topic_centroid_version(topic)
        previous = state.topic_centroid_versions.get(topic.id)
        if previous is not None and previous != version:
            log.warning(
                "версия центроида темы %s изменилась с прошлого запуска: %s -> %s "
                "(грани темы правились между запусками)",
                topic.id,
                previous,
                version,
            )
        state.topic_centroid_versions[topic.id] = version


class StateStore:
    """Загрузка/сохранение state.json с атомарной записью."""

    def __init__(self, path: Path, logger: logging.Logger | None = None) -> None:
        self._path = path
        self._logger = logger or logging.getLogger(__name__)

    def load(self) -> StateData:
        if not self._path.exists():
            # §8: файла не было — это законный первый запуск (или новый
            # источник), а не потеря состояния. ERROR здесь был бы ложной
            # тревогой; старт с текущего момента — штатное поведение.
            self._logger.info("%s не найден: первый запуск, старт с текущего момента", self._path)
            return StateData()
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            self._quarantine(f"{self._path} повреждён ({exc})")
            return StateData()
        try:
            return StateData.model_validate(data)
        except (ValidationError, TypeError) as exc:
            self._quarantine(f"{self._path} не соответствует схеме ({exc})")
            return StateData()

    def _quarantine(self, reason: str) -> None:
        # §8 v1.9: испорченный файл не затирается валидным при следующей
        # записи — переименовывается в `<имя>.bad`, иначе разбирать причину
        # порчи после факта будет не по чему.
        self._log_state_loss(reason)
        bad_path = self._path.with_name(f"{self._path.name}.bad")
        try:
            self._path.replace(bad_path)
        except OSError as exc:
            self._logger.error(
                "не удалось переименовать %s в %s (%s), испорченный файл остался на месте",
                self._path,
                bad_path,
                exc,
            )

    def _log_state_loss(self, reason: str) -> None:
        # §8: файл был, но испорчен/не читается — это настоящая потеря
        # состояния, а не штатный старт, поэтому ERROR.
        # TODO(пакет 6): уведомление в служебный канал (service_chat).
        self._logger.error(
            "%s: last_message_id потеряны по всем источникам, старт с текущего момента, "
            "история добора не восстанавливается",
            reason,
        )

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
