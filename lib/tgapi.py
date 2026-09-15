# -*- coding: utf-8 -*-
"""Работа с Telegram через Telethon (MTProto, ваш собственный аккаунт).

Импорт telethon — ленивый, чтобы selftest и веб-панель работали без него.
"""
import asyncio
import logging
import os

from . import paths

log = logging.getLogger("doomsday.tgapi")


class TgError(Exception):
    pass


class SessionNotAuthorized(TgError):
    pass


def session_path(cfg: dict) -> str:
    name = (cfg.get("telegram", {}).get("session") or "doomsday").strip() or "doomsday"
    return os.path.join(paths.SESSION_DIR, name)


def _client_ctor(cfg: dict):
    from telethon import TelegramClient  # ленивый импорт

    api_id = int(cfg.get("telegram", {}).get("api_id") or 0)
    api_hash = str(cfg.get("telegram", {}).get("api_hash") or "")
    if not api_id or not api_hash or "•" in api_hash:
        raise TgError("telegram.api_id / api_hash не настроены (doomsday setup)")
    paths.ensure_dirs()
    return TelegramClient(session_path(cfg), api_id, api_hash)


async def connect(cfg: dict, interactive: bool = False):
    """Подключиться и вернуть авторизованный клиент."""
    client = _client_ctor(cfg)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            if not interactive:
                raise SessionNotAuthorized(
                    "Сессия Telegram не авторизована. Запустите: doomsday login"
                )
            await _interactive_login(client, cfg)
    except Exception:
        await client.disconnect()
        raise
    return client


async def _interactive_login(client, cfg: dict) -> None:
    """Интерактивный вход: телефон → код → 2FA."""
    phone = str(cfg.get("telegram", {}).get("phone") or "").strip()
    if not phone:
        phone = input("Телефон в международном формате (например +79991234567): ").strip()
    await client.send_code_request(phone)
    code = input("Код из Telegram: ").strip()
    try:
        await client.sign_in(phone, code)
    except Exception as e:  # вероятно, включена двухфакторка
        if "password" not in str(e).lower() and "SRP" not in str(e):
            raise
        pwd = input("Пароль двухфакторной аутентификации: ")
        await client.sign_in(password=pwd)


async def get_bot(client, cfg: dict):
    username = (cfg.get("telegram", {}).get("game_bot") or "").lstrip("@").strip()
    if not username:
        raise TgError("telegram.game_bot не задан")
    try:
        return await client.get_entity(username)
    except ValueError:
        raise TgError(f"Не удалось найти бота @{username} (проверьте username в настройках)")


async def resolve_webview_url(client, bot, cfg: dict) -> str:
    """Получить URL Mini App игры вместе с авторизационными данными (tgWebAuthData).

    Порядок попыток:
      1) RequestAppWebView с short_name приложения (t.me/<bot>/<app_short_name>);
      2) RequestWebView через кнопку меню бота (main app, from_bot_menu).
    """
    short = (cfg.get("telegram", {}).get("app_short_name") or "").strip()
    last_err = None
    if short:
        try:
            from telethon.tl.functions.messages import RequestAppWebViewRequest
            from telethon.tl.types import InputBotAppShortName

            app = InputBotAppShortName(bot_id=bot, short_name=short)
            res = await client(RequestAppWebViewRequest(
                peer=bot,
                app=app,
                platform="android",
                write_allowed=True,
            ))
            if getattr(res, "url", None):
                return res.url
        except Exception as e:
            last_err = e
            log.info("RequestAppWebView(%s) не сработал: %s — пробуем from_bot_menu", short, e)
    try:
        from telethon.tl.functions.messages import RequestWebViewRequest

        res = await client(RequestWebViewRequest(
            peer=bot,
            bot=bot,
            platform="android",
            from_bot_menu=True,
        ))
        if getattr(res, "url", None):
            return res.url
    except Exception as e:
        last_err = e
    raise TgError(f"Не удалось получить URL Mini App игры: {last_err}")


async def send_start(client, bot, text: str = "/start"):
    """Отправить сообщение боту (по умолчанию /start — обновляет связь с ботом)."""
    return await client.send_message(bot, text)


async def read_bot_messages(client, bot, limit: int = 20, after_id: int = 0):
    """Последние сообщения бота в чате. Возвращает (список от старых к новым, max_id)."""
    msgs = await client.get_messages(bot, limit=max(1, int(limit)))
    out = []
    max_id = after_id
    for m in reversed(msgs):  # get_messages отдаёт от новых к старым
        if m.id <= after_id:
            continue
        if m.message:
            out.append({
                "id": m.id,
                "date": m.date.isoformat() if m.date else None,
                "text": m.message,
            })
        max_id = max(max_id, m.id)
    return out, max_id


def run(coro):
    """Запустить корутину в свежем event loop (удобно для CLI)."""
    return asyncio.run(coro)
