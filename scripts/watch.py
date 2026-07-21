"""Режим наблюдения (пакеты 2-4): Reader + Matcher + Deduplicator, без публикации.

Пост идёт Reader → MatchingSink (отбор по темам, §5) → Deduplicator (§6) →
LoggingSink — Publisher ещё нет (пакет 5). Не точка входа для боевого запуска
под systemd: она появится в пакете 7. Session-файл должен уже существовать
(см. scripts/login.py).

Запуск:
    python scripts/watch.py --config config/config.yaml --state state.json
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

from telethon import TelegramClient

from tg_monitor.config_store import ConfigStore
from tg_monitor.deduplicator import Deduplicator
from tg_monitor.embedder import SentenceTransformerEmbedder
from tg_monitor.logging_setup import setup_logging
from tg_monitor.matcher import Matcher, MatchingSink
from tg_monitor.reader import LoggingSink, TelegramReader, run_with_graceful_shutdown
from tg_monitor.state import StateStore, reconcile_topic_centroid_versions
from tg_monitor.telegram_env import load_api_credentials

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("config/config.yaml"), help="путь к config.yaml"
    )
    parser.add_argument("--state", type=Path, default=Path("state.json"), help="путь к state.json")
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    config_store = ConfigStore(args.config)
    bundle = config_store.get()
    setup_logging(
        bundle.config.logging.level, bundle.config.logging.file, bundle.config.logging.timezone
    )

    api_id, api_hash = load_api_credentials()
    session_path = Path(bundle.config.account.session_path).expanduser()
    client = TelegramClient(str(session_path), api_id, api_hash, connection_retries=None)

    embedder = SentenceTransformerEmbedder(
        model=bundle.config.embedder.model,
        cache_dir=bundle.config.embedder.cache_dir,
        device=bundle.config.embedder.device,
    )
    matcher = Matcher(embedder=embedder, config_store=config_store)

    # §8 v1.9: state.json читается один раз здесь — сверка версий центроидов
    # идёт по этому же объекту, который затем передаётся в Reader и
    # Deduplicator как есть, без повторного чтения с диска (было хрупко:
    # порядок load() в watch.py и в Reader имел значение сам по себе). Оба
    # компонента делят один объект `state` и один `StateStore`, независимо
    # сохраняя его при своих изменениях (last_message_id и dedup_buffer
    # соответственно).
    state_store = StateStore(args.state)
    state = state_store.load()
    reconcile_topic_centroid_versions(state, bundle.topics, logger)
    state_store.save(state)

    # Reader → MatchingSink (§5) → Deduplicator (§6) → LoggingSink.
    deduplicator = Deduplicator(
        config_store=config_store,
        state_store=state_store,
        state=state,
        sink=LoggingSink(tz=ZoneInfo(bundle.config.logging.timezone)),
    )
    matching_sink = MatchingSink(matcher=matcher, sink=deduplicator)

    reader = TelegramReader(
        client=client,
        config_store=config_store,
        state_store=state_store,
        state=state,
        sink=matching_sink,
    )
    await run_with_graceful_shutdown(reader)


if __name__ == "__main__":
    asyncio.run(_main())
