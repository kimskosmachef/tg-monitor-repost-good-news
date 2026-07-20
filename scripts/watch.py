"""Режим наблюдения (пакет 2): Reader без публикации.

Печатает нормализованные посты в лог через LoggingSink — Publisher ещё нет
(пакет 5). Не точка входа для боевого запуска под systemd: она появится в
пакете 7. Session-файл должен уже существовать (см. scripts/login.py).

Запуск:
    python scripts/watch.py --config config/config.yaml --state state.json
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from zoneinfo import ZoneInfo

from telethon import TelegramClient

from tg_monitor.config_store import ConfigStore
from tg_monitor.logging_setup import setup_logging
from tg_monitor.reader import LoggingSink, TelegramReader, run_with_graceful_shutdown
from tg_monitor.state import StateStore
from tg_monitor.telegram_env import load_api_credentials


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

    reader = TelegramReader(
        client=client,
        config_store=config_store,
        state_store=StateStore(args.state),
        sink=LoggingSink(tz=ZoneInfo(bundle.config.logging.timezone)),
    )
    await run_with_graceful_shutdown(reader)


if __name__ == "__main__":
    asyncio.run(_main())
