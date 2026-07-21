"""Ошибки конфигурации. Каждая указывает файл и (где применимо) путь к полю."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError
from pydantic_core import ErrorDetails


class ConfigError(Exception):
    """Базовая ошибка конфигурации."""


class ConfigParseError(ConfigError):
    """Файл — не валидный YAML."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: ошибка разбора YAML: {reason}")


class ConfigValidationError(ConfigError):
    """Файл разобран, но не прошёл схему."""

    def __init__(self, path: Path, validation_error: ValidationError) -> None:
        self.path = path
        self.validation_error = validation_error
        details = "; ".join(_format_pydantic_error(e) for e in validation_error.errors())
        super().__init__(f"{path}: {details}")


class ExamplesFileError(ConfigError):
    """Файл примеров/негативов (§4.2) не читается или (для примеров) пуст."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


def _format_pydantic_error(err: ErrorDetails) -> str:
    loc = ".".join(str(part) for part in err["loc"]) if err["loc"] else "<root>"
    return f"{loc}: {err['msg']}"
