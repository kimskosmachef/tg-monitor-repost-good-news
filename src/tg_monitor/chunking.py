"""Чанкование поста для оценки Matcher'ом — §5.2 docs/spec.md.

Длинный пост, усреднённый целиком, размывает тему: релевантный абзац тонет
в нерелевантных. Поэтому пост режется по абзацам (`\\n\\n`), короткие абзацы
клеятся к соседнему, длинные режутся принудительно. Score поста считается
как максимум по чанкам (§5.1), не среднее — это уже забота Matcher'а, не
этого модуля.
"""

from __future__ import annotations


def chunk_text(text: str, *, min_chunk_chars: int, max_chunk_chars: int) -> list[str]:
    """Разбить текст поста на чанки по абзацам — §5.2.

    Пустой текст (после strip — пустые "абзацы" из повторных `\\n\\n`) даёт
    пустой список: пост без текста не оценивается вовсе (§5.3), это решает
    вызывающий код, здесь только чанкование непустого текста.
    """
    paragraphs = [p.strip() for p in text.split("\n\n")]
    paragraphs = [p for p in paragraphs if p]
    if not paragraphs:
        return []
    merged = _merge_short_paragraphs(paragraphs, min_chunk_chars)
    chunks: list[str] = []
    for paragraph in merged:
        chunks.extend(_split_long_paragraph(paragraph, max_chunk_chars))
    return chunks


def _merge_short_paragraphs(paragraphs: list[str], min_chars: int) -> list[str]:
    # Абзацы короче min_chunk_chars клеятся к соседнему (§5.2): накапливаем
    # буфер вперёд, пока он не наберёт минимальную длину, затем фиксируем его
    # как чанк. Хвост короче порога, оставшийся после последнего абзаца,
    # приклеивается назад — к последнему зафиксированному чанку, а если
    # чанков ещё не было (весь текст короче порога), становится единственным.
    merged: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        buffer = f"{buffer}\n\n{paragraph}" if buffer else paragraph
        if len(buffer) >= min_chars:
            merged.append(buffer)
            buffer = ""
    if buffer:
        if merged:
            merged[-1] = f"{merged[-1]}\n\n{buffer}"
        else:
            merged.append(buffer)
    return merged


def _split_long_paragraph(paragraph: str, max_chars: int) -> list[str]:
    # Длиннее max_chunk_chars — режется принудительно по этому лимиту (§5.2).
    if len(paragraph) <= max_chars:
        return [paragraph]
    return [paragraph[i : i + max_chars] for i in range(0, len(paragraph), max_chars)]
