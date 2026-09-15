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


async def find_game_bot_in_dialogs(client, patterns=("doom", "tyranny")):
    """Поиск бота игры в диалогах пользователя.

    Надёжнее username из конфига: если вы хоть раз открывали игру, чат бота
    есть в диалогах — и мы найдём настоящего бота, даже если в настройках
    опечатка или указан канал/чат сообщества.
    """
    import re as _re
    rx = [_re.compile(p, _re.I) for p in patterns]
    found, seen = [], set()
    for folder in (None, 1):  # основной список диалогов + архив
        try:
            async for d in client.iter_dialogs(folder=folder, limit=300):
                e = d.entity
                if not getattr(e, "bot", False) or getattr(e, "id", None) in seen:
                    continue
                hay = " ".join(filter(None, [
                    getattr(e, "username", None) or "",
                    getattr(e, "first_name", None) or "",
                    getattr(e, "last_name", None) or "",
                    d.name or ""]))
                if any(p.search(hay) for p in rx):
                    seen.add(getattr(e, "id", id(e)))
                    found.append(e)
        except Exception as e:
            log.info("iter_dialogs(folder=%s): %s", folder, e)
    return found


async def get_bot(client, cfg: dict):
    """Сущность бота игры.

    Порядок поиска:
      1) username из конфига (с проверкой, что это действительно бот);
      2) штатный @DoomsDayTyrannybot;
      3) поиск в диалогах по шаблону doom/tyranny.
    """
    configured = (cfg.get("telegram", {}).get("game_bot") or "").lstrip("@").strip()
    tried = []

    for username in dict.fromkeys(filter(None, [configured, "DoomsDayTyrannybot"])):
        try:
            ent = await client.get_entity(username)
        except Exception as e:
            tried.append(f"@{username}: {type(e).__name__}")
            log.info("get_entity(%s): %s", username, e)
            continue
        if getattr(ent, "bot", False):
            if username != configured:
                log.info("game_bot: используем @%s (в конфиге было: %s)",
                         getattr(ent, "username", username), configured or "—")
            return ent
        kind = "канал/чат" if getattr(ent, "broadcast", None) is not None else "пользователь (не бот)"
        tried.append(f"@{username} — {kind}")
        log.warning("telegram.game_bot=%s указывает на %s — ищем настоящего бота", username, kind)

    log.info("Ищу бота игры в диалогах…")
    cands = await find_game_bot_in_dialogs(client)
    if len(cands) == 1:
        log.info("Бот игры найден в диалогах: @%s", getattr(cands[0], "username", "?"))
        return cands[0]
    if len(cands) > 1:
        names = ", ".join("@" + (getattr(c, "username", None) or str(getattr(c, "id", "?")))
                          for c in cands)
        raise TgError(f"В диалогах найдено несколько ботов игры: {names}. "
                      "Укажите точный telegram.game_bot в настройках (веб-панель → Настройки)")
    raise TgError(
        "Бот игры не найден (" + "; ".join(tried)[:200] + "). Откройте игру в Telegram "
        "хотя бы один раз (t.me/DoomsDayTyrannybot/play) и повторите, либо задайте "
        "верный telegram.game_bot в настройках.")


async def _webapp_button_url(client, bot):
    """URL web-приложения из последних кнопок бота (кнопка «Играть»).

    Совместимо со старым и новым представлением кнопок в Telethon:
    старое — KeyboardButtonWebView(url=...); новое (1.45+) — единая
    KeyboardButton(type=ButtonTypeSimpleWebView(url=...)) и
    KeyboardInlineButton(type=InlineButtonTypeWebView(url=...)).
    """
    try:
        msgs = await client.get_messages(bot, limit=30)
    except Exception as e:
        log.info("get_messages(кнопки): %s", e)
        return None
    fallback = None
    for m in msgs:
        for row in (m.buttons or []):
            for b in row:
                btype = getattr(b, "type", None)
                url = getattr(b, "url", None) or getattr(btype, "url", None)
                if not url:
                    continue
                cls = type(btype if btype is not None else b).__name__
                if "WebView" in cls:
                    return url
                if fallback is None:
                    fallback = url
    return fallback


async def _bot_inputs(client, bot):
    """Явная конвертация entity бота в InputPeer/InputUser для raw-запросов.

    ВАЖНО: Telethon НЕ конвертирует полные entity автоматически при прямых
    вызовах TL-конструкторов — без этого в поле InputUser уезжает «сырой»
    конструктор User, и сервер отвечает BOT_APP_BOT_INVALID /
    «This is not a valid bot» / URL_INVALID, хотя бот совершенно валиден.
    """
    from telethon import utils as tl_utils
    from telethon.tl.types import InputPeerUser

    peer = await client.get_input_entity(bot)
    try:
        iuser = tl_utils.get_input_user(bot)
    except Exception:
        iuser = None
    if iuser is None and isinstance(peer, InputPeerUser):
        from telethon.tl.types import InputUser
        iuser = InputUser(user_id=peer.user_id, access_hash=peer.access_hash)
    if iuser is None:
        raise TgError("не удалось получить InputUser для бота "
                      f"@{getattr(bot, 'username', '?')}")
    return peer, iuser


async def resolve_webview_url(client, bot, cfg: dict) -> str:
    """Получить URL Mini App игры вместе с авторизационными данными (tgWebAuthData).

    Порядок попыток:
      1) RequestAppWebView с short_name приложения (t.me/<bot>/<app_short_name>);
      2) RequestWebView через кнопку меню бота (main app, from_bot_menu);
      3) RequestWebView с URL из web-кнопки в последних сообщениях бота.
    Все запросы идут с явной конвертацией entity в InputPeer/InputUser.
    """
    peer, iuser = await _bot_inputs(client, bot)
    short = (cfg.get("telegram", {}).get("app_short_name") or "").strip()
    last_err = None
    if short:
        try:
            from telethon.tl.functions.messages import RequestAppWebViewRequest
            from telethon.tl.types import InputBotAppShortName

            app = InputBotAppShortName(bot_id=iuser, short_name=short)
            res = await client(RequestAppWebViewRequest(
                peer=peer,
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
            peer=peer,
            bot=iuser,
            platform="android",
            from_bot_menu=True,
        ))
        if getattr(res, "url", None):
            return res.url
    except Exception as e:
        last_err = e
        log.info("RequestWebView(from_bot_menu) не сработал: %s — ищем web-кнопку", e)

    # 3) web-кнопка (например «Играть») из последних сообщений бота
    try:
        btn_url = await _webapp_button_url(client, bot)
        if btn_url:
            from telethon.tl.functions.messages import RequestWebViewRequest

            res = await client(RequestWebViewRequest(
                peer=peer, bot=iuser, platform="android", url=btn_url))
            if getattr(res, "url", None):
                return res.url
    except Exception as e:
        last_err = e
        log.info("RequestWebView(web-кнопка) не сработал: %s", e)
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
