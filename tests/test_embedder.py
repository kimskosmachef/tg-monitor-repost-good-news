from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from tg_monitor.embedder import SentenceTransformerEmbedder


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
