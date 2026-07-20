"""Первичная интерактивная авторизация Telegram-аккаунта — §4, §10 docs/spec.md.

Отдельно от основного процесса: под systemd интерактивный ввод (код,
облачный пароль) невозможен. Запускается один раз вручную, создаёт
session-файл по пути из `config.account.session_path` и выставляет на
него права 600.

Запуск:
    python scripts/login.py --config config/config.yaml

Перед запуском в .env должны быть заданы TG_API_ID и TG_API_HASH
(см. .env.example).
"""

from __future__ import annotations

import argparse
import asyncio
import stat
from pathlib import Path

from telethon import TelegramClient

from tg_monitor.config_loader import load_config
from tg_monitor.telegram_env import load_api_credentials

_SESSION_EXTENSION = ".session"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("config/config.yaml"), help="путь к config.yaml"
    )
    return parser.parse_args()


def _session_file_path(session_path: Path) -> Path:
    name = str(session_path)
    if not name.endswith(_SESSION_EXTENSION):
        name += _SESSION_EXTENSION
    return Path(name)


async def _main() -> None:
    args = _parse_args()
    config = load_config(args.config)
    api_id, api_hash = load_api_credentials()

    session_path = Path(config.account.session_path).expanduser()
    session_path.parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(str(session_path), api_id, api_hash)
    await client.start()  # запросит номер телефона, код и облачный пароль (2FA)
    await client.disconnect()

    session_file = _session_file_path(session_path)
    session_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(f"Готово: session-файл сохранён в {session_file}, права выставлены в 600.")


if __name__ == "__main__":
    asyncio.run(_main())
