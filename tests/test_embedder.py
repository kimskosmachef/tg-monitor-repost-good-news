from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np
import pytest

from tg_monitor.embedder import SentenceTransformerEmbedder, _warn_on_truncation_batch


class FakeTokenizer:
    """Токенизатор-заглушка: без torch/sentence-transformers, просто считает вызовы.

    `vectors` — заранее известная токенизация каждого текста (id — просто длина
    в "токенах", условная), реального BPE тут нет, важно только число вызовов.
    """

    def __init__(self, token_ids_by_text: dict[str, list[int]]) -> None:
        self._token_ids_by_text = token_ids_by_text
        self.calls = 0

    def __call__(self, texts: list[str], *, add_special_tokens: bool) -> dict[str, list[list[int]]]:
        self.calls += 1
        return {"input_ids": [self._token_ids_by_text[text] for text in texts]}

    def decode(self, token_ids: list[int], *, skip_special_tokens: bool) -> str:
        return f"decoded:{token_ids}"


# --- _warn_on_truncation_batch: один батч-вызов, не по тексту, пункт 5 -------


def test_warn_on_truncation_tokenizes_batch_once_not_per_text() -> None:
    tokenizer = FakeTokenizer({"короткий": [1, 2], "длинный текст": [1, 2, 3, 4, 5]})

    _warn_on_truncation_batch(tokenizer, ["короткий", "длинный текст"], max_len=10)

    assert tokenizer.calls == 1


def test_warn_on_truncation_logs_only_texts_over_limit(caplog: pytest.LogCaptureFixture) -> None:
    tokenizer = FakeTokenizer({"короткий": [1, 2], "длинный текст": [1, 2, 3, 4, 5]})

    with caplog.at_level(logging.WARNING):
        _warn_on_truncation_batch(tokenizer, ["короткий", "длинный текст"], max_len=3)

    assert len(caplog.records) == 1
    assert "5 токенов" in caplog.text
    assert "лимите модели 3" in caplog.text


def test_warn_on_truncation_empty_texts_skips_tokenizer_call() -> None:
    tokenizer = FakeTokenizer({})

    _warn_on_truncation_batch(tokenizer, [], max_len=10)

    assert tokenizer.calls == 0


@pytest.mark.slow
def test_real_model_returns_normalized_vectors(tmp_path: Path) -> None:
    """Интеграционный тест: качает реальную модель, требует сети — §11 промпта пакета 3.

    Не входит в обычный гейт `pytest` (см. `addopts` в pyproject.toml),
    запускается явно: `pytest -m slow`.
    """
    start = time.monotonic()
    embedder = SentenceTransformerEmbedder(
        model="paraphrase-multilingual-mpnet-base-v2",
        cache_dir=str(tmp_path),
        device="cpu",
    )
    load_seconds = time.monotonic() - start
    print(f"\nвремя загрузки модели: {load_seconds:.2f} c")

    vectors = embedder.embed(["привет, мир", "hello, world", ""])

    assert len(vectors) == 3
    for vector in vectors:
        assert np.linalg.norm(vector) == pytest.approx(1.0, abs=1e-3)

    assert embedder.embed([]) == []
