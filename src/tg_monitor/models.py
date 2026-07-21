"""Модели данных конфигурации и постов — §4, §4.1 docs/spec.md."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Literal

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

# §5.5: мягкий порог по умолчанию для тем в shadow-режиме (threshold: null).
# Узаконено спекой v1.7 (было предложением пакета 3, см. отчёт по пакету).
DEFAULT_SOFT_THRESHOLD = 0.2


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
    """Грань темы со своим набором примеров и центроидом — §5.1, §4.2.

    `examples_file` — путь к текстовому файлу примеров (§4.2), разрешается
    относительно каталога `topics.yaml`. `examples` — уже загруженное из
    этого файла содержимое: не часть схемы `topics.yaml`, заполняется
    `config_loader.load_topics()` после чтения файла, поэтому исключено из
    сериализации (`exclude=True`) и не валидируется как входное поле.
    """

    id: str
    examples_file: str
    examples: list[str] = Field(default_factory=list, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_inline_examples(cls, data: Any) -> Any:
        # §4.2: старый формат (инлайн-список в topics.yaml) — не молчаливо
        # игнорируется и не переопределяется файлом, а ошибка конфига, как и
        # требует StrictModel. config_loader заполняет `examples` уже после
        # валидации, присваиванием атрибута — сюда это не попадает.
        if isinstance(data, dict) and "examples" in data:
            raise ValueError(
                "examples задаётся только через examples_file (§4.2) — "
                "инлайн-список в topics.yaml больше не поддерживается"
            )
        return data


class Topic(StrictModel):
    """Тема: грани, целевой канал, порог, источники — §4, §5."""

    id: str
    target: str
    sources: Literal["all"] | list[str] = "all"
    threshold: float | None = None
    # §5.5: применяется только пока threshold: null (shadow-режим) — отсекает
    # явный шум, не заменяет калибровку. Узаконено спекой v1.7.
    soft_threshold: float = DEFAULT_SOFT_THRESHOLD
    chunk_strategy: ChunkStrategy = "paragraph"
    facets: list[Facet] = Field(min_length=1)
    # §4.2: негативы опциональны на уровне темы — отсутствие поля означает
    # отсутствие второго центроида (§5.4), а не ошибку.
    negatives_file: str | None = None
    # Загружено из negatives_file config_loader'ом — см. docstring Facet.examples.
    negatives: list[str] = Field(default_factory=list, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_inline_negatives(cls, data: Any) -> Any:
        # §4.2, §5.4 — тот же принцип, что и Facet._reject_inline_examples.
        if isinstance(data, dict) and "negatives" in data:
            raise ValueError(
                "negatives задаётся только через negatives_file (§4.2) — "
                "инлайн-список в topics.yaml больше не поддерживается"
            )
        return data


def warn_if_facet_examples_below_recommended(
    topic_id: str, facet_id: str, examples_file: str, examples: list[str]
) -> None:
    """Предупредить, если у грани меньше рекомендованных примеров — §5.1, §4.2.

    Число примеров — рекомендация, а не валидация (тот же принцип, что и у
    `boost` в §5.6), поэтому грань всё равно загружается. Вызывается
    `config_loader.load_topics()` после чтения `examples_file`: до этого
    момента число примеров неизвестно, оно приходит из файла, а не из
    `topics.yaml`.
    """
    if len(examples) < FACET_MIN_EXAMPLES:
        logger.warning(
            "тема %s, грань %s (%s): %d примеров меньше рекомендованных %d",
            topic_id,
            facet_id,
            examples_file,
            len(examples),
            FACET_MIN_EXAMPLES,
        )


class AccountConfig(StrictModel):
    """Учётная запись юзербота — §4."""

    session_path: str


class LoggingConfig(StrictModel):
    """Логирование — файл + stdout, уровень и таймзона меток времени из конфига — §4."""

    level: str = "INFO"
    file: str = "logs/tg-monitor.log"
    timezone: str = "Europe/Riga"


class EmbedderConfig(StrictModel):
    """Секция `embedder` — §3, §4, §5.2 docs/spec.md."""

    model: str = "paraphrase-multilingual-mpnet-base-v2"
    cache_dir: str = "~/.tg-monitor/models"
    device: str = "cpu"
    # §5.2: длиннее — режется принудительно, короче — приклеивается к соседнему.
    # 400 — под окно модели в 128 токенов (§5.2, спека v1.7): более длинный
    # чанк токенизатор усекает молча, см. предупреждение в Embedder.
    max_chunk_chars: int = Field(default=400, gt=0)
    min_chunk_chars: int = Field(default=40, gt=0)

    @model_validator(mode="after")
    def _check_chunk_bounds(self) -> EmbedderConfig:
        if self.min_chunk_chars >= self.max_chunk_chars:
            raise ValueError(
                f"min_chunk_chars ({self.min_chunk_chars}) должен быть меньше "
                f"max_chunk_chars ({self.max_chunk_chars})"
            )
        return self


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
    embedder: EmbedderConfig = Field(default_factory=EmbedderConfig)


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
    # Reader-диагностика: пост получен через live events.NewMessage или через
    # периодический добор истории — без этой метки регрессия в подписке на
    # события (пост едет только доборот) остаётся незаметной по логу.
    origin: Literal["live", "catchup"]
