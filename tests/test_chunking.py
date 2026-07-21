from __future__ import annotations

from tg_monitor.chunking import chunk_text

# Небольшие пороги, чтобы примеры оставались короткими и читаемыми.
MIN = 10
MAX = 30


def test_paragraphs_above_min_stay_separate() -> None:
    text = "первый абзац достаточно длинный\n\nвторой абзац тоже достаточно длинный"
    chunks = chunk_text(text, min_chunk_chars=MIN, max_chunk_chars=1000)
    assert chunks == ["первый абзац достаточно длинный", "второй абзац тоже достаточно длинный"]


def test_short_leading_paragraph_merges_forward() -> None:
    # "коротко" (7 символов) короче MIN=10 — клеится к следующему абзацу.
    text = "коротко\n\nдостаточно длинный абзац рядом"
    chunks = chunk_text(text, min_chunk_chars=MIN, max_chunk_chars=1000)
    assert chunks == ["коротко\n\nдостаточно длинный абзац рядом"]


def test_short_trailing_paragraph_merges_backward() -> None:
    # Последний абзац короче MIN, соседа впереди нет — клеится к предыдущему
    # уже зафиксированному чанку.
    text = "длинный первый абзац текста\n\nхвост"
    chunks = chunk_text(text, min_chunk_chars=MIN, max_chunk_chars=1000)
    assert chunks == ["длинный первый абзац текста\n\nхвост"]


def test_single_short_paragraph_stays_alone() -> None:
    # Весь текст короче min_chunk_chars и клеить не к чему — остаётся один чанк.
    text = "коротко"
    chunks = chunk_text(text, min_chunk_chars=MIN, max_chunk_chars=1000)
    assert chunks == ["коротко"]


def test_long_paragraph_is_force_split_by_max_chunk_chars() -> None:
    text = "а" * 75
    chunks = chunk_text(text, min_chunk_chars=MIN, max_chunk_chars=MAX)
    assert chunks == ["а" * MAX, "а" * MAX, "а" * (75 - 2 * MAX)]


def test_paragraph_exactly_at_max_chunk_chars_is_not_split() -> None:
    text = "б" * MAX
    chunks = chunk_text(text, min_chunk_chars=MIN, max_chunk_chars=MAX)
    assert chunks == ["б" * MAX]


def test_merge_then_split_when_merged_buffer_exceeds_max() -> None:
    # Два коротких абзаца клеятся друг к другу (§5.2), а следующий абзац
    # длиннее max_chunk_chars режется принудительно следующим шагом.
    text = f"{'x' * 6}\n\n{'y' * 6}\n\n{'z' * 45}"
    chunks = chunk_text(text, min_chunk_chars=MIN, max_chunk_chars=MAX)
    merged_first = f"{'x' * 6}\n\n{'y' * 6}"
    assert chunks[0] == merged_first
    assert "".join(chunks[1:]) == "z" * 45


def test_empty_text_yields_no_chunks() -> None:
    assert chunk_text("", min_chunk_chars=MIN, max_chunk_chars=MAX) == []


def test_only_blank_paragraphs_yield_no_chunks() -> None:
    assert chunk_text("\n\n\n\n   \n\n", min_chunk_chars=MIN, max_chunk_chars=MAX) == []


def test_whitespace_around_paragraphs_is_stripped() -> None:
    text = "  первый абзац с пробелами  \n\n  второй абзац с пробелами  "
    chunks = chunk_text(text, min_chunk_chars=MIN, max_chunk_chars=1000)
    assert chunks == ["первый абзац с пробелами", "второй абзац с пробелами"]


# --- разрез длинного абзаца по границе предложения/слова, §5.2 v1.8 ---------


def test_long_paragraph_splits_on_sentence_boundary_in_last_third() -> None:
    # MAX=30, последняя треть — символы [20:30). Точка на позиции 24 (внутри
    # трети) — режем сразу после неё, а не по символу на 30-й позиции.
    first = "а" * 24 + "." + "б" * 5  # длина 30: точка на индексе 24
    text = f"{first} хвост абзаца, который идёт дальше"
    chunks = chunk_text(text, min_chunk_chars=MIN, max_chunk_chars=MAX)
    assert chunks[0] == "а" * 24 + "."
    assert chunks[1].startswith("б" * 5)


def test_long_paragraph_splits_on_word_boundary_without_sentence_end() -> None:
    # Нет точки/!/?/… нигде в окне — ищем последний пробел во всём окне
    # (не только в последней трети) и режем по нему, без разрыва слова.
    text = "слово " * 6 + "хвост"  # 41 символ, без точек
    chunks = chunk_text(text, min_chunk_chars=MIN, max_chunk_chars=MAX)
    assert chunks == ["слово слово слово слово слово", "слово хвост"]


def test_long_word_without_any_boundary_is_hard_cut_by_char() -> None:
    # Слово само длиннее лимита — единственный случай разреза по символу.
    text = "ы" * 75
    chunks = chunk_text(text, min_chunk_chars=MIN, max_chunk_chars=MAX)
    assert chunks == ["ы" * MAX, "ы" * MAX, "ы" * (75 - 2 * MAX)]
