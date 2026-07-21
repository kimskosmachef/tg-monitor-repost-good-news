"""Горячая перезагрузка конфигов по mtime — §4, §4.2.

`ConfigStore.get()` всегда отдаёт последнюю валидную версию каждого из трёх
файлов независимо: если один файл сломан, остальные два всё равно
перезагружаются при изменении, а битый остаётся на предыдущей валидной
версии до починки (§9: "Некорректный topics.yaml: старый конфиг остаётся
в силе, ошибка в лог"). Темы дополнительно следят за mtime файлов примеров
и негативов (§4.2): правка файла примеров подхватывается так же, как
правка самого `topics.yaml`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tg_monitor.config_loader import examples_paths_for, load_config, load_sources, load_topics
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


def _stat_paths(paths: list[Path]) -> dict[Path, float]:
    # Отсутствующий файл — тоже часть отслеживаемого состояния: появление
    # ранее отсутствовавшего файла примеров должно быть замечено не хуже,
    # чем правка существующего.
    mtimes: dict[Path, float] = {}
    for path in paths:
        try:
            mtimes[path] = path.stat().st_mtime
        except OSError:
            mtimes[path] = -1.0
    return mtimes


@dataclass
class _TrackedTopics:
    """Как `_TrackedFile[list[Topic]]`, но следит ещё и за файлами примеров (§4.2)."""

    path: Path
    value: list[Topic]
    mtime: float
    example_mtimes: dict[Path, float] = field(default_factory=dict)
    # Пока файл примеров не почтен, mtime не даёт покоя maybe_reload на каждом
    # get() (Matcher вызывает его на каждом посте) — без дедупликации одна и
    # та же ошибка спамила бы лог на каждый пост, пока конфиг не почтен.
    _last_error: str | None = field(default=None, repr=False)

    @classmethod
    def load_initial(cls, path: Path) -> _TrackedTopics:
        value = load_topics(path)
        return cls(
            path=path,
            value=value,
            mtime=path.stat().st_mtime,
            example_mtimes=_stat_paths(examples_paths_for(value, path.parent)),
        )

    def maybe_reload(self, logger: logging.Logger) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError as exc:
            logger.error("не удалось прочитать mtime %s: %s", self.path, exc)
            return
        current_example_mtimes = _stat_paths(examples_paths_for(self.value, self.path.parent))
        if mtime == self.mtime and current_example_mtimes == self.example_mtimes:
            return
        try:
            new_value = load_topics(self.path)
        except ConfigError as exc:
            reason = str(exc)
            if reason != self._last_error:
                logger.error(
                    "%s не применён, использую предыдущую валидную версию: %s", self.path, exc
                )
                self._last_error = reason
            return
        self.value = new_value
        self.mtime = mtime
        self.example_mtimes = _stat_paths(examples_paths_for(new_value, self.path.parent))
        self._last_error = None


class ConfigStore:
    """Держит актуальные config.yaml, topics.yaml, sources.yaml с hot-reload."""

    def __init__(self, config_path: Path, logger: logging.Logger | None = None) -> None:
        self._config_path = config_path
        self._base_dir = config_path.parent
        self._logger = logger or logging.getLogger(__name__)

        self._config_file: _TrackedFile[Config] = _TrackedFile.load_initial(
            config_path, load_config
        )
        self._topics_file: _TrackedTopics = _TrackedTopics.load_initial(
            self._resolve(self._config_file.value.topics_file)
        )
        self._sources_file: _TrackedFile[list[Source]] = _TrackedFile.load_initial(
            self._resolve(self._config_file.value.sources_file), load_sources
        )

    def get(self) -> ConfigBundle:
        self._config_file.maybe_reload(self._logger)
        self._resync_topics_path(self._config_file.value.topics_file)
        self._resync_dependent_path(self._sources_file, self._config_file.value.sources_file)
        self._topics_file.maybe_reload(self._logger)
        self._sources_file.maybe_reload(self._logger)
        return ConfigBundle(
            config=self._config_file.value,
            topics=self._topics_file.value,
            sources=self._sources_file.value,
            sources_path=self._sources_file.path,
        )

    def _resync_topics_path(self, configured_name: str) -> None:
        """Если config.yaml поменял имя файла тем — перечитать (включая файлы примеров)."""
        new_path = self._resolve(configured_name)
        if new_path == self._topics_file.path:
            return
        try:
            new_value = load_topics(new_path)
        except ConfigError as exc:
            self._logger.error(
                "%s не применён, использую предыдущую валидную версию: %s", new_path, exc
            )
            return
        self._topics_file.path = new_path
        self._topics_file.value = new_value
        self._topics_file.mtime = new_path.stat().st_mtime
        self._topics_file.example_mtimes = _stat_paths(
            examples_paths_for(new_value, new_path.parent)
        )
        self._topics_file._last_error = None

    def _resync_dependent_path[T](self, tracked: _TrackedFile[T], configured_name: str) -> None:
        """Если config.yaml поменял имя файла источников — перечитать с новым путём."""
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
