"""Чанкование поста для оценки Matcher'ом — §5.2 docs/spec.md.

Длинный пост, усреднённый целиком, размывает тему: релевантный абзац тонет
в нерелевантных. Поэтому пост режется по абзацам (`\\n\\n`), короткие абзацы
клеятся к соседнему, длинные режутся принудительно. Score поста считается
как максимум по чанкам (§5.1), не среднее — это уже забота Matcher'а, не
этого модуля.
"""

from __future__ import annotations

# §5.2: разрыв абзаца по предложению — только эти символы считаются концом
# предложения (многоточие включено отдельным символом и тройкой точек сразу).
_SENTENCE_END_CHARS = ".!?…"


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
    # Длиннее max_chunk_chars — режется принудительно (§5.2), но не всегда
    # ровно по лимиту символов: сперва пробуем границу предложения в
    # последней трети окна, потом границу слова во всём окне, и только если
    # ни одной границы нет (само слово длиннее лимита) — режем по символу.
    if len(paragraph) <= max_chars:
        return [paragraph]
    chunks: list[str] = []
    remaining = paragraph
    while len(remaining) > max_chars:
        cut = _find_cut(remaining, max_chars)
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _find_cut(text: str, max_chars: int) -> int:
    window = text[:max_chars]

    third_start = max_chars - max_chars // 3
    sentence_cut = -1
    for i in range(third_start, max_chars):
        if window[i] in _SENTENCE_END_CHARS:
            sentence_cut = i
    if sentence_cut != -1:
        return sentence_cut + 1

    word_cut = window.rfind(" ")
    if word_cut != -1:
        return word_cut

    # Само слово длиннее max_chars — единственный случай, где режем по
    # символу (§5.2: разрыв посреди слова портит токенизацию).
    return max_chars
