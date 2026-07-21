"""Embedder — обёртка над эмбеддинг-моделью, §3, §4 (`embedder:`) docs/spec.md.

Единственное место в проекте, знающее про sentence-transformers. Остальной
код (Matcher, калибровка) работает только с векторами `numpy.ndarray` через
узкий интерфейс `Embedder` — §3/§10: вынос модели в отдельный сервис не
должен задеть код, который её вызывает.

Модель грузится один раз при создании `SentenceTransformerEmbedder` (в
конструкторе), не лениво при первом вызове — старт процесса явно платит
за загрузку, а не первый обработанный пост.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Protocol

import numpy as np
import numpy.typing as npt

logger = logging.getLogger(__name__)

Vector = npt.NDArray[np.float32]

# encode() сам режет список текстов на батчи такого размера — единственный
# батч-параметр, отдельного поля в конфиге под него нет (§4 его не описывает).
_DEFAULT_BATCH_SIZE = 32


class Embedder(Protocol):
    """Узкий интерфейс: текст → нормализованный вектор, батчами.

    Подменяется в тестах фейком с детерминированными векторами — ни один
    тест Matcher/чанкования не должен качать модель или лезть в сеть.
    """

    def embed(self, texts: Sequence[str]) -> list[Vector]: ...


class SentenceTransformerEmbedder:
    """Реализация `Embedder` поверх `sentence-transformers` (§3, §4)."""

    def __init__(self, model: str, cache_dir: str, device: str) -> None:
        # Импорт внутри конструктора: sentence-transformers/torch — тяжёлая
        # зависимость, нужная только здесь, а не всем, кто просто типизирует
        # Embedder-протокол.
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model, cache_folder=cache_dir, device=device)

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        if not texts:
            return []
        self._warn_on_truncation(texts)
        vectors = self._model.encode(
            list(texts),
            batch_size=_DEFAULT_BATCH_SIZE,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [np.asarray(vector, dtype=np.float32) for vector in vectors]

    def _warn_on_truncation(self, texts: Sequence[str]) -> None:
        # §5.2 (спека v1.7): токенизатор молча режет вход длиннее окна модели
        # (128 токенов) — без предупреждения деградация невидима. Считаем
        # длину без урезания (add_special_tokens=True, без truncation) и
        # сравниваем с max_seq_length модели; сработавший случай логируется
        # с усечённым текстом и числом токенов до урезания.
        max_len = self._model.max_seq_length
        if max_len is None:
            return
        _warn_on_truncation_batch(self._model.tokenizer, texts, max_len)


def _warn_on_truncation_batch(tokenizer: Any, texts: Sequence[str], max_len: int) -> None:
    # Один батч-вызов токенизатора на весь список чанков вместо цикла с
    # вызовом на каждый текст по отдельности — раньше один и тот же список
    # чанков токенизировался по одному тексту за раз (N вызовов), хотя
    # быстрый токенизатор одинаково умеет принять список целиком за один
    # проход.
    if not texts:
        return
    batch = tokenizer(list(texts), add_special_tokens=True)["input_ids"]
    for token_ids in batch:
        if len(token_ids) <= max_len:
            continue
        truncated_text: str = tokenizer.decode(token_ids[:max_len], skip_special_tokens=True)
        logger.warning(
            "токенизатор усекает вход: %d токенов при лимите модели %d, усечённый текст: %r",
            len(token_ids),
            max_len,
            truncated_text,
        )
