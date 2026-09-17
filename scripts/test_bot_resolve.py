# -*- coding: utf-8 -*-
"""Юнит-тесты умного резолва бота игры (tgapi) без сети — на моках."""
import asyncio
import sys
import types

sys.path.insert(0, "/home/z/my-project/doomsday-bot")

from telethon.tl.types import (User, Channel, KeyboardButton, KeyboardInlineButton,
                               ButtonTypeSimpleWebView, InlineButtonTypeUrl, InputPeerUser,
                               InputUser)
import binascii


def _cid_le(cls) -> str:
    """Constructor id класса TLObject в виде little-endian hex (как в потоке)."""
    return binascii.hexlify(cls.CONSTRUCTOR_ID.to_bytes(4, "little")).decode()


USER_CID = _cid_le(User)
INPUTUSER_CID = _cid_le(InputUser)

from lib import tgapi


# ---------- моки ----------

def mk_user(uid, username, first="", bot=False):
    return User(id=uid, is_self=False, contact=False, mutual_contact=False, deleted=False,
                bot=bot, bot_chat_history=False, bot_nochats=False, verified=False,
                restricted=False, min=False, bot_inline_geo=False, support=False,
                scam=False, apply_min_photo=True, fake=False, bot_attach_menu=False,
                premium=False, attach_menu_enabled=False, bot_can_edit=False,
                close_friend=False, stories_hidden=False, stories_unavailable=False,
                contact_require_premium=False, bot_business=False,
                bot_has_main_app=bot, bot_forum_view=False,
                bot_forum_can_manage_topics=False, bot_can_manage_bots=False,
                bot_guestchat=False, bot_guard=False,
                first_name=first, status=None, username=username,
                access_hash=-9000000000000000000, bot_info_version=0 if bot else None)

def mk_channel(cid, username, title):
    return Channel(id=cid, title=title, username=username, access_hash=-1,
                   photo=None, date=None, creator=False, restricted=False,
                   verified=False, scam=False, fake=False, gigagroup=False,
                   broadcast=True)

def mk_dialog(entity, name):
    d = types.SimpleNamespace()
    d.entity = entity
    d.name = name
    return d

GAME_BOT   = mk_user(101, "DoomsDayTyrannybot", "DoomsDay Tyranny", bot=True)
SUPPORT_BOT = mk_user(102, "DoomsdayTyranny_support_bot", "DT Support", bot=True)
NEWS_CH    = mk_channel(201, "DoomsdayTyranny", "Doomsday Tyranny News")
OTHER_BOT  = mk_user(103, "unrelated_bot", "Shop", bot=True)
HUMAN      = mk_user(104, "some_guy", "Ivan", bot=False)


class MockClient:
    """Имитация Telethon-клиента: get_entity + iter_dialogs + get_messages.

    ВАЖНО: __call__ СЕРИАЛИЗИРУЕТ запрос (bytes(request)) — как настоящий
    клиент. Это ловит баги передачи полных entity вместо InputUser/InputPeer.
    """
    def __init__(self, entities_by_name, dialogs, messages=None):
        self._ents = entities_by_name   # {"username": entity}
        self._dialogs = dialogs         # [dialog]
        self._msgs = messages or []
        self.requests = []              # сериализированные запросы (для проверок)

    async def get_entity(self, username):
        username = username.lstrip("@")
        if username not in self._ents:
            raise ValueError(f"No user with username {username}")
        return self._ents[username]

    async def get_input_entity(self, entity):
        # как Telethon: entity -> InputPeer-форма
        if isinstance(entity, InputPeerUser):
            return entity
        return InputPeerUser(user_id=entity.id, access_hash=entity.access_hash)

    def iter_dialogs(self, folder=None, limit=None):
        main = [d for d in self._dialogs if folder in (None, 0)]
        arch = [d for d in self._dialogs if getattr(d, "archived", False)]
        # имитация Telethon: folder=1 — архив, None — основной список (без архива)
        seq = arch if folder == 1 else [d for d in main if not getattr(d, "archived", False)]
        async def gen():
            for d in seq[:limit if limit else 10**9]:
                yield d
        return gen()

    async def get_messages(self, bot, limit=30):
        return self._msgs[:limit]

    async def __call__(self, request):
        data = bytes(request)  # сериализация — обязательный этап реального вызова
        self.requests.append((type(request).__name__, data))
        # в запросе не должно быть «сырого» конструктора User
        assert USER_CID not in binascii.hexlify(data).decode(), \
            "в запрос уехал сырой User вместо InputUser/InputPeer!"
        r = types.SimpleNamespace()
        r.url = "https://mockwebview.example/tma?tgWebAppData=signed"
        return r


def mk_msg(buttons=None):
    m = types.SimpleNamespace()
    m.buttons = buttons
    return m


# ---------- тесты ----------

def test_config_points_to_channel():
    """Случай пользователя: game_bot в конфиге — канал. Должны найти бота."""
    client = MockClient(
        {"DoomsdayTyranny": NEWS_CH, "DoomsDayTyrannybot": GAME_BOT},
        dialogs=[mk_dialog(NEWS_CH, "Doomsday Tyranny News"),
                 mk_dialog(GAME_BOT, "DoomsDay Tyranny"),
                 mk_dialog(OTHER_BOT, "Shop")],
    )
    cfg = {"telegram": {"game_bot": "@DoomsdayTyranny"}}  # канал, не бот
    bot = asyncio.run(tgapi.get_bot(client, cfg))
    assert bot.username == "DoomsDayTyrannybot", bot.username
    print("  ok  конфиг указывает на канал -> найден @DoomsDayTyrannybot")


def test_config_ok():
    client = MockClient(
        {"DoomsDayTyrannybot": GAME_BOT},
        dialogs=[mk_dialog(GAME_BOT, "DoomsDay Tyranny")],
    )
    cfg = {"telegram": {"game_bot": "@DoomsDayTyrannybot"}}
    bot = asyncio.run(tgapi.get_bot(client, cfg))
    assert bot.username == "DoomsDayTyrannybot"
    print("  ok  корректный конфиг")


def test_dialogs_only():
    """Конфиг пуст/битый, get_entity падает — бот ищется в диалогах (в архиве!)."""
    client = MockClient(
        {},
        dialogs=[mk_dialog(OTHER_BOT, "Shop"),
                 mk_dialog(GAME_BOT, "DoomsDay Tyranny")],
    )
    # пометим диалог игры архивным
    client._dialogs[1].archived = True
    cfg = {"telegram": {"game_bot": "@nonexistent"}}
    bot = asyncio.run(tgapi.get_bot(client, cfg))
    assert bot.username == "DoomsDayTyrannybot"
    print("  ok  бот найден в архивных диалогах")


def test_multiple_bots_error():
    client = MockClient(
        {},
        dialogs=[mk_dialog(GAME_BOT, "DoomsDay Tyranny"),
                 mk_dialog(SUPPORT_BOT, "DT Support")],
    )
    cfg = {"telegram": {"game_bot": ""}}
    try:
        asyncio.run(tgapi.get_bot(client, cfg))
        raise AssertionError("ожидалась TgError")
    except tgapi.TgError as e:
        assert "несколько ботов" in str(e)
    print("  ok  несколько кандидатов -> понятная ошибка")


def test_nothing_found():
    client = MockClient({}, dialogs=[mk_dialog(OTHER_BOT, "Shop"), mk_dialog(HUMAN, "Ivan")])
    cfg = {"telegram": {"game_bot": "@whatever"}}
    try:
        asyncio.run(tgapi.get_bot(client, cfg))
        raise AssertionError("ожидалась TgError")
    except tgapi.TgError as e:
        assert "не найден" in str(e).lower()
    print("  ok  ничего не найдено -> понятная ошибка")


def test_webapp_button_url():
    """Web-кнопка «Играть» из сообщений бота извлекается (новый формат 1.45+)."""
    msgs = [
        mk_msg(buttons=[[KeyboardInlineButton(text="Канал",
                                              type=InlineButtonTypeUrl(url="https://t.me/news"))]]),
        mk_msg(buttons=[[KeyboardButton(text="Играть",
                                        type=ButtonTypeSimpleWebView(url="https://game.example/app"))]]),
    ]
    client = MockClient({}, dialogs=[], messages=msgs)
    url = asyncio.run(tgapi._webapp_button_url(client, GAME_BOT))
    assert url == "https://game.example/app", url
    print("  ok  web-кнопка извлечена из сообщений")


def test_webapp_button_only_links():
    """Нет web-кнопок, только обычные ссылки — отдаём первую как fallback."""
    msgs = [mk_msg(buttons=[[KeyboardInlineButton(
        text="Сайт", type=InlineButtonTypeUrl(url="https://example.com"))]])]
    client = MockClient({}, dialogs=[], messages=msgs)
    url = asyncio.run(tgapi._webapp_button_url(client, GAME_BOT))
    assert url == "https://example.com", url
    print("  ok  fallback на обычную ссылку")


def test_resolve_webview_fallback_to_button():
    """short_name и from_bot_menu падают -> резолв через кнопку бота.

    Мок-клиент кидает исключение на первые два запроса, а на третий
    (RequestWebView с url) возвращает нормальный ответ.
    """
    class FailingFirst(MockClient):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.calls = 0
        async def __call__(self, request):
            data = bytes(request)  # сериализация обязательна
            self.requests.append((type(request).__name__, data))
            assert USER_CID not in binascii.hexlify(data).decode(), \
                "в запрос уехал сырой User вместо InputUser/InputPeer!"
            self.calls += 1
            cname = type(request).__name__
            if cname == "RequestAppWebViewRequest":
                raise Exception("BOT_APP_BOT_INVALID")
            if cname == "RequestWebViewRequest" and not getattr(request, "url", None):
                raise Exception("This is not a valid bot")
            r = types.SimpleNamespace()
            r.url = "https://game.example/app?tgWebAppData=signed"
            return r

    msgs = [mk_msg(buttons=[[KeyboardButton(text="Играть",
                                             type=ButtonTypeSimpleWebView(url="https://game.example/app"))]])]
    client = FailingFirst({}, dialogs=[], messages=msgs)
    cfg = {"telegram": {"app_short_name": "play"}}
    url = asyncio.run(tgapi.resolve_webview_url(client, GAME_BOT, cfg))
    assert "tgWebAppData" in url, url
    assert client.calls == 3, client.calls
    print("  ok  webview через web-кнопку (3-я стратегия)")


def test_input_user_serialization():
    """Главная регрессия: в raw-запросах должны быть InputUser/InputPeer,
    а не полный User (причина BOT_APP_BOT_INVALID / 'not a valid bot').
    """
    client = MockClient({}, dialogs=[])
    cfg = {"telegram": {"app_short_name": "play"}}
    asyncio.run(tgapi.resolve_webview_url(client, GAME_BOT, cfg))
    names = [n for n, _ in client.requests]
    assert "RequestAppWebViewRequest" in names, names
    hexes = "".join(binascii.hexlify(d).decode() for _, d in client.requests)
    assert INPUTUSER_CID in hexes, "InputUser не найден в сериализации запросов"
    assert USER_CID not in hexes, "сырой User уехал в запрос!"
    print("  ok  сериализация: InputUser в запросах, сырого User нет")


if __name__ == "__main__":
    print("test_config_points_to_channel:")
    test_config_points_to_channel()
    print("test_config_ok:")
    test_config_ok()
    print("test_dialogs_only:")
    test_dialogs_only()
    print("test_multiple_bots_error:")
    test_multiple_bots_error()
    print("test_nothing_found:")
    test_nothing_found()
    print("test_webapp_button_url:")
    test_webapp_button_url()
    print("test_webapp_button_only_links:")
    test_webapp_button_only_links()
    print("test_resolve_webview_fallback_to_button:")
    test_resolve_webview_fallback_to_button()
    print("test_input_user_serialization:")
    test_input_user_serialization()
    print("\nВсе тесты резолва бота пройдены ✔")
