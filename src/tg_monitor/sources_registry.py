"""Автоматическая правка реестра источников — §4.1, §9 docs/spec.md.

Reader сам проставляет `status: unavailable`, если аккаунт потерял доступ
к каналу. Правится файл на диске (не только in-memory состояние), чтобы
следующая перезагрузка config.yaml по mtime (§4) видела актуальный статус
и не пыталась переподписаться. Round-trip через ruamel.yaml, а не
`yaml.safe_dump`, чтобы не терять комментарии и форматирование остальных
записей — reader.py правит этот файл автоматически, без участия человека.
Запись атомарная (tempfile + os.replace), как в state.py, — падение
процесса посреди записи не должно оставить sources.yaml битым или пустым.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from ruamel.yaml import YAML

_yaml = YAML()
_yaml.preserve_quotes = True


def mark_source_unavailable(path: Path, source_id: str, logger: logging.Logger) -> None:
    """Проставить status: unavailable записи `source_id` в sources.yaml на диске."""
    try:
        with path.open(encoding="utf-8") as stream:
            data = _yaml.load(stream)
    except OSError as exc:
        logger.error(
            "не удалось прочитать %s, чтобы пометить источник %s unavailable: %s",
            path,
            source_id,
            exc,
        )
        return

    entry = next((item for item in data or [] if item.get("id") == source_id), None)
    if entry is None:
        logger.error(
            "источник %s не найден в %s, статус unavailable не проставлен", source_id, path
        )
        return

    entry["status"] = "unavailable"
    try:
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                _yaml.dump(data, tmp_file)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            Path(tmp_name).replace(path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    except OSError as exc:
        logger.error(
            "не удалось записать %s со статусом unavailable для %s: %s", path, source_id, exc
        )
        return
    logger.warning("источник %s помечен status: unavailable в %s", source_id, path)
