"""Разбор и валидация config.yaml, topics.yaml, sources.yaml — §4, §4.1."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter, ValidationError

from tg_monitor.errors import ConfigParseError, ConfigValidationError
from tg_monitor.models import Config, Source, Topic

_topics_adapter = TypeAdapter(list[Topic])
_sources_adapter = TypeAdapter(list[Source])


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


def load_topics(path: Path) -> list[Topic]:
    """Загрузить и провалидировать topics.yaml (плоский список тем)."""
    data = _read_yaml(path)
    try:
        return _topics_adapter.validate_python(data or [])
    except ValidationError as exc:
        raise ConfigValidationError(path, exc) from exc


def load_sources(path: Path) -> list[Source]:
    """Загрузить и провалидировать sources.yaml (плоский список источников)."""
    data = _read_yaml(path)
    try:
        return _sources_adapter.validate_python(data or [])
    except ValidationError as exc:
        raise ConfigValidationError(path, exc) from exc
