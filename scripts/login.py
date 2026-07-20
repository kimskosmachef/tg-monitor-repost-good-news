"""Первичная интерактивная авторизация Telegram-аккаунта — §4, §10 docs/spec.md.

Отдельно от основного процесса: под systemd интерактивный ввод (QR, код,
облачный пароль) невозможен. Запускается один раз вручную, создаёт
session-файл по пути из `config.account.session_path` и выставляет на
него права 600.

Основной путь — вход по QR-коду: Telegram не присылает код новому
аккаунту (ни в приложение, ни SMS, ни звонком), только сканирование QR
уже авторизованным клиентом. Вход по коду — запасной вариант под --phone.

Запуск (QR, основной путь):
    python scripts/login.py --config config/config.yaml

Запуск (код, запасной вариант):
    python scripts/login.py --config config/config.yaml --phone +371XXXXXXXX

Перед запуском в .env должны быть заданы TG_API_ID и TG_API_HASH
(см. .env.example).
"""

from __future__ import annotations

import argparse
import asyncio
import stat
import sys
from pathlib import Path

import qrcode
from telethon import TelegramClient, errors

from tg_monitor.config_loader import load_config
from tg_monitor.telegram_env import load_api_credentials

_SESSION_EXTENSION = ".session"

# Имена классов telethon.tl.types.auth.SentCodeType* — человекочитаемое описание
# способа доставки кода (§ "печатать SentCodeType в режиме --phone").
_SENT_CODE_TYPE_DESCRIPTIONS: dict[str, str] = {
    "SentCodeTypeApp": "код в приложении Telegram",
    "SentCodeTypeSms": "код по SMS",
    "SentCodeTypeCall": "код голосовым звонком",
    "SentCodeTypeFlashCall": "код флеш-звонком (номер звонящего — часть кода)",
    "SentCodeTypeMissedCall": "код пропущенным звонком (номер звонящего — часть кода)",
    "SentCodeTypeFragmentSms": "код по SMS через Fragment",
    "SentCodeTypeEmailCode": "код на почту",
    "SentCodeTypeSetUpEmailRequired": "аккаунт требует привязки почты для входа",
    "SentCodeTypeSmsPhrase": "фраза по SMS",
    "SentCodeTypeSmsWord": "слово по SMS",
    "SentCodeTypeFirebaseSms": "код по SMS (проверка Firebase)",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config/config.yaml"), help="путь к config.yaml"
    )
    parser.add_argument(
        "--phone",
        type=str,
        default=None,
        help="запасной вариант: вход по коду на этот номер телефона, вместо QR",
    )
    return parser.parse_args()


def _session_file_path(session_path: Path) -> Path:
    name = str(session_path)
    if not name.endswith(_SESSION_EXTENSION):
        name += _SESSION_EXTENSION
    return Path(name)


def _print_qr(url: str) -> None:
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make()
    qr.print_ascii(tty=sys.stdout.isatty())
    print(f"Если QR не читается — ссылка для tg://: {url}")


async def _prompt_cloud_password(client: TelegramClient) -> None:
    password = input("Включена облачная 2FA-защита, введите пароль: ")
    await client.sign_in(password=password)


async def _login_qr(client: TelegramClient) -> None:
    qr_login = await client.qr_login()
    while True:
        _print_qr(qr_login.url)
        print(f"Ожидание сканирования (QR истекает в {qr_login.expires:%H:%M:%S} UTC)…")
        try:
            await qr_login.wait()
            return
        except TimeoutError:
            print("QR-код истёк, генерирую новый.")
            await qr_login.recreate()
        except errors.SessionPasswordNeededError:
            await _prompt_cloud_password(client)
            return


async def _login_phone(client: TelegramClient, phone: str) -> None:
    sent = await client.send_code_request(phone)
    description = _SENT_CODE_TYPE_DESCRIPTIONS.get(
        type(sent.type).__name__, type(sent.type).__name__
    )
    print(f"Способ доставки кода: {description}")
    code = input("Код из Telegram: ")
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
    except errors.SessionPasswordNeededError:
        await _prompt_cloud_password(client)


async def _main() -> None:
    args = _parse_args()
    config = load_config(args.config)
    api_id, api_hash = load_api_credentials()

    session_path = Path(config.account.session_path).expanduser()
    session_path.parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(str(session_path), api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            try:
                if args.phone:
                    await _login_phone(client, args.phone)
                else:
                    await _login_qr(client)
            except errors.RPCError as exc:
                print(f"Ошибка Telegram: {type(exc).__name__}: {exc}", file=sys.stderr)
                raise SystemExit(1) from exc
    finally:
        await client.disconnect()

    session_file = _session_file_path(session_path)
    session_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(f"Готово: session-файл сохранён в {session_file}, права выставлены в 600.")


if __name__ == "__main__":
    asyncio.run(_main())
