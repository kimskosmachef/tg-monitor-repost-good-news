"""Офлайн-калибровка порогов — §5.5, пункт 10 промпта пакета 3.

Прогоняет посты из файла через Embedder и Matcher без Reader и Telegram и
печатает таблицу сырых score по темам и граням — основной инструмент
подбора `threshold`/`soft_threshold` перед боевым запуском (§5.5). Ничего
никуда не публикует и не пишет в state.json.

Формат входного файла — JSON Lines, один пост на строку:

    {"text": "текст поста", "source_id": "src_a"}
    {"text": "ещё один пост"}

`source_id` необязателен: если задан и найден в sources.yaml — в таблице
учитывается его boost (§5.6), иначе boost считается нулевым. Пустые строки,
битый JSON и записи без "text" пропускаются с пометкой в выводе — молчаливых
пропусков нет (CLAUDE.md).

Запуск:
    python scripts/score.py --config config/config.yaml --posts posts.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tg_monitor.chunking import chunk_text
from tg_monitor.config_store import ConfigStore
from tg_monitor.embedder import SentenceTransformerEmbedder
from tg_monitor.matcher import CentroidStore, facet_scores, negative_score, source_boost

_PREVIEW_CHARS = 70


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config/config.yaml"), help="путь к config.yaml"
    )
    parser.add_argument(
        "--posts", type=Path, required=True, help="JSONL-файл с постами, см. описание выше"
    )
    return parser.parse_args()


def _load_posts(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"строка {line_no}: битый JSON, пропущена ({exc})")
            continue
        if not isinstance(parsed, dict) or not parsed.get("text"):
            print(f"строка {line_no}: без поля text, пропущена")
            continue
        parsed["_line"] = line_no
        records.append(parsed)
    return records


def _print_post_table(
    record: dict[str, object],
    bundle_topics_scored: list[tuple[str, dict[str, float], float, bool]],
    boost: float,
) -> None:
    header = f"{'тема':<28}{'грань':<22}{'raw':>8}{'adj':>8}{'final':>8}   "
    print(header)
    print("-" * len(header))
    for topic_id, positive, sim_negative, has_negative in bundle_topics_scored:
        best_facet_id = max(
            positive,
            key=lambda fid: positive[fid] - sim_negative if has_negative else positive[fid],
        )
        for facet_id in sorted(positive):
            raw = positive[facet_id]
            adjusted = raw - sim_negative if has_negative else raw
            final = adjusted + boost
            marker = "*" if facet_id == best_facet_id else ""
            print(
                f"{topic_id:<28}{facet_id:<22}{raw:>8.4f}{adjusted:>8.4f}{final:>8.4f}   {marker}"
            )


def main() -> None:
    args = _parse_args()
    config_store = ConfigStore(args.config)
    bundle = config_store.get()

    posts = _load_posts(args.posts)
    if not posts:
        print("нет постов для оценки")
        return

    embedder = SentenceTransformerEmbedder(
        model=bundle.config.embedder.model,
        cache_dir=bundle.config.embedder.cache_dir,
        device=bundle.config.embedder.device,
    )
    centroid_store = CentroidStore(embedder)

    for record in posts:
        text = str(record["text"])
        source_id = record.get("source_id")
        boost = source_boost(bundle.sources, str(source_id)) if source_id else 0.0
        chunks = chunk_text(
            text,
            min_chunk_chars=bundle.config.embedder.min_chunk_chars,
            max_chunk_chars=bundle.config.embedder.max_chunk_chars,
        )
        preview = text.replace("\n", " ")[:_PREVIEW_CHARS]
        ellipsis = "…" if len(text) > _PREVIEW_CHARS else ""
        print(
            f"\n=== запись {record['_line']} | чанков={len(chunks)} "
            f"| source={source_id or '-'} | boost={boost:.3f}"
        )
        print(f"    {preview}{ellipsis}")

        if not chunks:
            print("    без текста после чанкования (§5.3), пропущена")
            continue

        chunk_vectors = embedder.embed(chunks)
        scored: list[tuple[str, dict[str, float], float, bool]] = []
        for topic in bundle.topics:
            centroids = centroid_store.get(topic)
            positive = facet_scores(centroids, chunk_vectors)
            sim_negative = negative_score(centroids, chunk_vectors)
            scored.append((topic.id, positive, sim_negative, centroids.negative is not None))
        _print_post_table(record, scored, boost)


if __name__ == "__main__":
    main()
