"""Горячая перезагрузка конфигов по mtime — §4.

`ConfigStore.get()` всегда отдаёт последнюю валидную версию каждого из трёх
файлов независимо: если один файл сломан, остальные два всё равно
перезагружаются при изменении, а битый остаётся на предыдущей валидной
версии до починки (§9: "Некорректный topics.yaml: старый конфиг остаётся
в силе, ошибка в лог").
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tg_monitor.config_loader import load_config, load_sources, load_topics
from tg_monitor.errors import ConfigError
from tg_monitor.models import Config, Source, Topic


@dataclass
class ConfigBundle:
    """Согласованный снимок всех трёх конфигов на момент вызова `get()`."""

    config: Config
    topics: list[Topic]
    sources: list[Source]
    sources_path: Path


@dataclass
class _TrackedFile[T]:
    path: Path
    loader: Callable[[Path], T]
    value: T
    mtime: float

    @classmethod
    def load_initial(cls, path: Path, loader: Callable[[Path], T]) -> _TrackedFile[T]:
        value = loader(path)
        return cls(path=path, loader=loader, value=value, mtime=path.stat().st_mtime)

    def maybe_reload(self, logger: logging.Logger) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError as exc:
            logger.error("не удалось прочитать mtime %s: %s", self.path, exc)
            return
        if mtime == self.mtime:
            return
        try:
            new_value = self.loader(self.path)
        except ConfigError as exc:
            logger.error("%s не применён, использую предыдущую валидную версию: %s", self.path, exc)
            return
        self.value = new_value
        self.mtime = mtime


class ConfigStore:
    """Держит актуальные config.yaml, topics.yaml, sources.yaml с hot-reload."""

    def __init__(self, config_path: Path, logger: logging.Logger | None = None) -> None:
        self._config_path = config_path
        self._base_dir = config_path.parent
        self._logger = logger or logging.getLogger(__name__)

        self._config_file: _TrackedFile[Config] = _TrackedFile.load_initial(
            config_path, load_config
        )
        self._topics_file: _TrackedFile[list[Topic]] = _TrackedFile.load_initial(
            self._resolve(self._config_file.value.topics_file), load_topics
        )
        self._sources_file: _TrackedFile[list[Source]] = _TrackedFile.load_initial(
            self._resolve(self._config_file.value.sources_file), load_sources
        )

    def get(self) -> ConfigBundle:
        self._config_file.maybe_reload(self._logger)
        self._resync_dependent_path(self._topics_file, self._config_file.value.topics_file)
        self._resync_dependent_path(self._sources_file, self._config_file.value.sources_file)
        self._topics_file.maybe_reload(self._logger)
        self._sources_file.maybe_reload(self._logger)
        return ConfigBundle(
            config=self._config_file.value,
            topics=self._topics_file.value,
            sources=self._sources_file.value,
            sources_path=self._sources_file.path,
        )

    def _resync_dependent_path[T](self, tracked: _TrackedFile[T], configured_name: str) -> None:
        """Если config.yaml поменял имя файла тем/источников — перечитать с новым путём."""
        new_path = self._resolve(configured_name)
        if new_path == tracked.path:
            return
        try:
            new_value = tracked.loader(new_path)
        except ConfigError as exc:
            self._logger.error(
                "%s не применён, использую предыдущую валидную версию: %s", new_path, exc
            )
            return
        tracked.path = new_path
        tracked.value = new_value
        tracked.mtime = new_path.stat().st_mtime

    def _resolve(self, name: str) -> Path:
        path = Path(name)
        return path if path.is_absolute() else self._base_dir / path
