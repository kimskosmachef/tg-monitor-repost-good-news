"""Хранилище состояния — §8 docs/spec.md.

`state.json` — горячий файл, переписывается после каждого обработанного
поста: last_message_id по источникам, версия центроида каждой темы (хэш
от примеров). Держится маленьким специально — буфер дедупа в нём не
живёт (см. `DedupBufferData`/`DedupBufferStore` ниже), иначе каждая
запись last_message_id гоняла бы через диск мегабайты векторов, которые
не изменились.

Запись атомарная (tempfile + os.replace). Отсутствие или порча файла —
не падение, старт с текущего момента (last_message_id пустой).
Испорченный файл не затирается валидным при следующей записи — он
переименовывается в `<имя>.bad`, иначе посмертный разбор причины порчи
невозможен.

`dedup-buffer.json` живёт рядом со `state.json` (`default_dedup_buffer_path`)
и переносит те же гарантии атомарности, но с более мягкими правилами при
потере: см. `DedupBufferStore`.
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
    """Один вектор в кольцевом буфере дедупа — §6.

    `source_id`/`message_id` — идентификатор опубликованного поста, вектор
    которого здесь хранится: без них лог отброшенного дубля (§6, пакет 4,
    пункт 5) не может назвать, с каким постом произошло совпадение.
    """

    topic_id: str
    source_id: str
    message_id: int
    vector: list[float]
    ts: dt.datetime


class StateData(BaseModel):
    """Полная схема state.json — §8. Буфер дедупа сюда не входит: он в
    `DedupBufferData`, отдельном файле рядом (`dedup-buffer.json`)."""

    last_message_id: dict[str, int] = Field(default_factory=dict)
    topic_centroid_versions: dict[str, str] = Field(default_factory=dict)


class DedupBufferData(BaseModel):
    """Схема dedup-buffer.json — §8. Живёт отдельно от state.json: пишется
    только когда буфер меняется составом (пост прошёл тему), а не на
    каждый обработанный пост."""

    entries: list[DedupEntry] = Field(default_factory=list)


def default_dedup_buffer_path(state_path: Path) -> Path:
    """Путь к dedup-buffer.json рядом со state.json — §8: отдельным
    параметром запуска не заводится, всегда выводится из пути state.json."""
    return state_path.with_name("dedup-buffer.json")


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


def _atomic_write_text(path: Path, payload: str) -> None:
    """Запись файла через temp + os.replace — общая для state.json и dedup-buffer.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(payload)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        Path(tmp_name).replace(path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _quarantine_to_bad(path: Path, logger: logging.Logger) -> None:
    """Переименовать испорченный файл в `<имя>.bad`, не затирая его — общая
    для state.json и dedup-buffer.json (§8 v1.9)."""
    bad_path = path.with_name(f"{path.name}.bad")
    try:
        path.replace(bad_path)
    except OSError as exc:
        logger.error(
            "не удалось переименовать %s в %s (%s), испорченный файл остался на месте",
            path,
            bad_path,
            exc,
        )


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
        _quarantine_to_bad(self._path, self._logger)

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
        _atomic_write_text(self._path, state.model_dump_json(indent=2))


class DedupBufferStore:
    """Загрузка/сохранение dedup-buffer.json — §8.

    В отличие от `StateStore` потеря этого файла не фатальна: буфер не
    хранит last_message_id, только защиту от повторной публикации в окне
    `dedup_window_hours`. Отсутствие файла — INFO и пустой буфер (как и у
    state.json), но порча — тоже ERROR и карантин в `.bad`, только с более
    мягкой формулировкой: посты не теряются, снимается лишь защита от
    дублей на время до следующего разбора.
    """

    def __init__(self, path: Path, logger: logging.Logger | None = None) -> None:
        self._path = path
        self._logger = logger or logging.getLogger(__name__)

    def load(self) -> DedupBufferData:
        if not self._path.exists():
            self._logger.info(
                "%s не найден: старт с пустым буфером дедупа (не фатально)", self._path
            )
            return DedupBufferData()
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            self._quarantine(f"{self._path} повреждён ({exc})")
            return DedupBufferData()
        try:
            return DedupBufferData.model_validate(data)
        except (ValidationError, TypeError) as exc:
            self._quarantine(f"{self._path} не соответствует схеме ({exc})")
            return DedupBufferData()

    def _quarantine(self, reason: str) -> None:
        self._logger.error(
            "%s: буфер дедупа потерян, защита от дублей временно снята "
            "(посты не теряются, старт с пустым буфером)",
            reason,
        )
        _quarantine_to_bad(self._path, self._logger)

    def save(self, buffer: DedupBufferData) -> None:
        _atomic_write_text(self._path, buffer.model_dump_json(indent=2))
