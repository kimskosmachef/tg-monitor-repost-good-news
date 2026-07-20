"""Модели данных конфигурации и постов — §4, §4.1 docs/spec.md."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

SourceStatus = Literal["active", "paused", "unavailable"]
ChunkStrategy = Literal["paragraph"]

# §5.6: границы для boost источника — рекомендация, не валидация.
# 0.0 — надбавка не задана (по умолчанию).
BOOST_MIN = 0.02
BOOST_MAX = 0.05

# §5.1: рекомендованный минимум примеров на грань — рекомендация, не валидация.
FACET_MIN_EXAMPLES = 8


class StrictModel(BaseModel):
    """Общая база: лишние поля в конфиге — ошибка, а не молчаливый игнор."""

    model_config = ConfigDict(extra="forbid")


class Source(StrictModel):
    """Запись реестра источников — §4.1."""

    id: str
    ref: str
    status: SourceStatus = "active"
    tags: list[str] = Field(default_factory=list)
    boost: float = 0.0
    added: dt.date
    note: str = ""

    @field_validator("boost")
    @classmethod
    def _warn_boost_above_recommended_max(cls, value: float) -> float:
        # §5.6: диапазон — рекомендация, не валидация; отказ загружать конфиг
        # из-за boost — неверное поведение, поэтому только предупреждение.
        if value > BOOST_MAX:
            logger.warning(
                "boost=%s выше рекомендованного максимума %s, значение применяется как есть",
                value,
                BOOST_MAX,
            )
        return value


class Facet(StrictModel):
    """Грань темы со своим набором примеров и центроидом — §5.1."""

    id: str
    examples: list[str] = Field(min_length=1)


class Topic(StrictModel):
    """Тема: грани, целевой канал, порог, источники — §4, §5."""

    id: str
    target: str
    sources: Literal["all"] | list[str] = "all"
    threshold: float | None = None
    chunk_strategy: ChunkStrategy = "paragraph"
    facets: list[Facet] = Field(min_length=1)
    negatives: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _warn_facets_below_recommended_examples(self) -> Topic:
        # §5.1: число примеров в грани — рекомендация, не валидация, по тому
        # же правилу, что и boost в §5.6: грань с малым числом примеров
        # загружается, но пишет предупреждение с id темы и грани.
        # Пустая грань (0 примеров) — ошибка конфига, отсекается на уровне
        # Facet.examples (min_length=1) до этой проверки.
        for facet in self.facets:
            if len(facet.examples) < FACET_MIN_EXAMPLES:
                logger.warning(
                    "тема %s, грань %s: %d примеров меньше рекомендованных %d",
                    self.id,
                    facet.id,
                    len(facet.examples),
                    FACET_MIN_EXAMPLES,
                )
        return self


class AccountConfig(StrictModel):
    """Учётная запись юзербота — §4."""

    session_path: str


class LoggingConfig(StrictModel):
    """Логирование — файл + stdout, уровень и таймзона меток времени из конфига — §4."""

    level: str = "INFO"
    file: str = "logs/tg-monitor.log"
    timezone: str = "Europe/Riga"


class RuntimeConfig(StrictModel):
    """Секция `runtime` — §4."""

    catchup_interval_min: int = Field(gt=0)
    dedup_window_hours: int = Field(gt=0)
    dedup_threshold: float = Field(ge=0.0, le=1.0)
    publish_delay_sec: int = Field(ge=0)
    max_post_age_min: int = Field(gt=0)
    rate_limit_per_hour: int | None = None
    forward_reposts: bool = True
    # §4: окно сборки альбома из отдельных событий в один пост.
    media_group_flush_delay_sec: float = Field(default=2.0, gt=0)


class Config(StrictModel):
    """Корень `config.yaml` — §4."""

    account: AccountConfig
    service_chat: str
    sources_file: str = "sources.yaml"
    topics_file: str = "topics.yaml"
    runtime: RuntimeConfig
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


class Post(BaseModel):
    """Нормализованный пост источника — §3, §5.3, дополнено в пакете 2 (Reader)."""

    message_id: int
    source_id: str
    date: dt.datetime
    text: str | None = None
    grouped_id: int | None = None
    # §7: id всех элементов медиагруппы (для одиночного поста — список из
    # одного id) — без полного списка групповой форвард невозможен.
    message_ids: list[int] = Field(default_factory=list)
    is_repost: bool = False
    has_media: bool = False
    forward_forbidden: bool = False
