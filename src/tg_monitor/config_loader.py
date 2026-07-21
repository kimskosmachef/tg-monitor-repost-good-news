"""Разбор и валидация config.yaml, topics.yaml, sources.yaml — §4, §4.1, §4.2."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter, ValidationError

from tg_monitor.errors import ConfigParseError, ConfigValidationError, ExamplesFileError
from tg_monitor.models import Config, Source, Topic, warn_if_facet_examples_below_recommended

_topics_adapter = TypeAdapter(list[Topic])
_sources_adapter = TypeAdapter(list[Source])

# §4.2: разделитель записей в файле примеров — строка, состоящая ровно из
# трёх дефисов.
_EXAMPLES_SEPARATOR = "---"


def _read_yaml(path: Path) -> Any:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigParseError(path, str(exc)) from exc
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigParseError(path, str(exc)) from exc


def load_config(path: Path) -> Config:
    """Загрузить и провалидировать config.yaml."""
    data = _read_yaml(path)
    try:
        return Config.model_validate(data or {})
    except ValidationError as exc:
        raise ConfigValidationError(path, exc) from exc


def _resolve_relative(base_dir: Path, name: str) -> Path:
    path = Path(name)
    return path if path.is_absolute() else base_dir / path


def parse_examples_text(raw: str) -> list[str]:
    """Разобрать текст файла примеров на записи — §4.2.

    Построчно: строка, равная ровно `---`, закрывает текущую запись и
    начинает следующую. Работает и когда разделитель — первая строка файла,
    и при нескольких разделителях подряд (обе стороны дают пустую запись),
    и без завершающего перевода строки — разбор идёт по `str.split("\\n")`,
    последняя запись собирается и без него. Пустые записи (после обрезки
    пробелов по краям) отбрасываются.
    """
    entries: list[str] = []
    current: list[str] = []
    for line in raw.split("\n"):
        if line == _EXAMPLES_SEPARATOR:
            entries.append("\n".join(current))
            current = []
        else:
            current.append(line)
    entries.append("\n".join(current))
    return [entry.strip() for entry in entries if entry.strip()]


def _read_examples_file(path: Path) -> list[str]:
    """Прочитать и разобрать файл примеров грани — ошибка, если пуст или не читается (§4.2)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExamplesFileError(path, f"не удалось прочитать файл примеров: {exc}") from exc
    examples = parse_examples_text(raw)
    if not examples:
        raise ExamplesFileError(path, "файл примеров пуст (нет ни одной записи)")
    return examples


def _read_negatives_file(path: Path) -> list[str]:
    """Прочитать и разобрать файл негативов — пустой файл не ошибка, негативы опциональны (§4.2)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExamplesFileError(path, f"не удалось прочитать файл негативов: {exc}") from exc
    return parse_examples_text(raw)


def examples_paths_for(topics: list[Topic], base_dir: Path) -> list[Path]:
    """Все пути к файлам примеров/негативов, на которые ссылаются темы — для отслеживания mtime.

    Используется `ConfigStore`: правка файла примеров подхватывается так же,
    как правка `topics.yaml` (§4.2), поэтому нужно знать полный список путей,
    за mtime которых следить, не только сам `topics.yaml`.
    """
    paths: list[Path] = []
    for topic in topics:
        for facet in topic.facets:
            paths.append(_resolve_relative(base_dir, facet.examples_file))
        if topic.negatives_file:
            paths.append(_resolve_relative(base_dir, topic.negatives_file))
    return paths


def load_topics(path: Path) -> list[Topic]:
    """Загрузить и провалидировать topics.yaml (плоский список тем), включая файлы примеров §4.2.

    Пути `examples_file`/`negatives_file` разрешаются относительно каталога
    `topics.yaml`. Пустой или отсутствующий файл примеров — ошибка конфига;
    пустой файл негативов допустим (негативы опциональны, §5.4).
    """
    data = _read_yaml(path)
    try:
        topics = _topics_adapter.validate_python(data or [])
    except ValidationError as exc:
        raise ConfigValidationError(path, exc) from exc

    base_dir = path.parent
    for topic in topics:
        for facet in topic.facets:
            examples_path = _resolve_relative(base_dir, facet.examples_file)
            facet.examples = _read_examples_file(examples_path)
            warn_if_facet_examples_below_recommended(
                topic.id, facet.id, str(examples_path), facet.examples
            )
        if topic.negatives_file:
            negatives_path = _resolve_relative(base_dir, topic.negatives_file)
            topic.negatives = _read_negatives_file(negatives_path)
    return topics


def load_sources(path: Path) -> list[Source]:
    """Загрузить и провалидировать sources.yaml (плоский список источников)."""
    data = _read_yaml(path)
    try:
        return _sources_adapter.validate_python(data or [])
    except ValidationError as exc:
        raise ConfigValidationError(path, exc) from exc
