"""Секреты Telegram-приложения из .env — §4, §10 docs/spec.md.

api_id/api_hash — секрет уровня приложения (my.telegram.org), отдельный от
аккаунта (session-файл, путь к которому задаётся в config.yaml). Читаются
из .env, не из config.yaml.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv


class MissingApiCredentialsError(RuntimeError):
    """TG_API_ID / TG_API_HASH не заданы в окружении или .env."""


def load_api_credentials() -> tuple[int, str]:
    """Загрузить и провалидировать TG_API_ID/TG_API_HASH из .env / окружения."""
    load_dotenv()
    api_id_raw = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    if not api_id_raw or not api_hash:
        raise MissingApiCredentialsError(
            "TG_API_ID и TG_API_HASH обязательны — задайте их в .env (см. .env.example)"
        )
    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise MissingApiCredentialsError(
            f"TG_API_ID должен быть числом, получено: {api_id_raw!r}"
        ) from exc
    return api_id, api_hash
