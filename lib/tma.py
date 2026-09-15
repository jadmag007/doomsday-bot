# -*- coding: utf-8 -*-
"""Драйвер Telegram Mini App: HTTP-шаги к API игры + Discovery эндпоинтов.

Авторизация TMA строится на initData, который Telegram выдаёт в URL веб-приложения
(резолвится через Telethon — см. tgapi.resolve_webview_url).

Шаг HTTP-запроса (конфиг tma.steps_*):
{
  "name": "auth",                       // имя шага (для отчёта)
  "method": "POST",                     // GET/POST/PUT/PATCH/DELETE
  "url": "{{base_url}}/api/v1/auth",    // {{переменные}} из контекста
  "headers": {"Content-Type": "application/json", "Authorization": "tma {{init_data}}"},
  "body": {"initData": "{{init_data}}"},// объект JSON или строка; {{переменные}} внутри
  "extract": {"token": "token"},        // JSON-пути из ответа -> переменные контекста
  "save": "state_raw",                  // имя переменной для сырого текста ответа
  "expect_status": [200],               // допустимые статусы (иначе шаг/конвейер провален)
  "on_fail": "abort"                    // abort|continue
}

Контекст по умолчанию: init_data, webview_url, base_url, origin, now.
"""
import copy
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("doomsday.tma")


class TmaError(Exception):
    pass


# ---------------- разбор webview URL ----------------

def parse_webview_url(url: str) -> dict:
    """Вытащить параметры авторизации TMA из URL webview.

    Telegram подставляет данные в query и/или fragment:
    tgWebAuthData / user / auth_date / hash / tgWebVersion ...
    Возвращает словарь: init_data (полная строка параметров), origin, params.
    """
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    frag = dict(urllib.parse.parse_qsl(parts.fragment, keep_blank_values=True))
    params = {**query, **frag}

    init_data = (
        params.get("tgWebAuthData")
        or params.get("initData")
        or _compose_init_data(params)
        or ""
    )
    # tgWebAppData тоже встречается
    if not init_data and "tgWebAppData" in params:
        init_data = params["tgWebAppData"]

    origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""
    return {"init_data": init_data, "origin": origin, "params": params, "url": url}


def _compose_init_data(params: dict) -> str:
    keys = ("user", "auth_date", "query_id", "start_param", "chat_instance", "chat_type", "hash")
    if not any(k in params for k in keys):
        return ""
    return urllib.parse.urlencode({k: v for k, v in params.items() if k in keys})


# ---------------- встроенный пресет API Doomsday ----------------

# Протокол игры Doomsday Tyranny (реверс-инжиниринг web-приложения):
# Firebase Cloud Functions (callable). Каждый вызов — POST на
# {{base_url}}/<имяФункции> с телом {"data": {...аргументы, auth, session}},
# где auth = СЫРАЯ строка tgWebAppData (точно как её выдаёт Telegram),
# session = поле hash из неё. Ответ: {"result": ...} / {"error": ...}.
# Свежесть auth_data критична: просроченная подпись → "sessionExpired",
# поэтому каждый проход заново резолвит webview URL через Telethon.
FIREBASE_PRESET = {
    "version": 1,
    "base_url": "https://us-central1-telegram-miracle-f1779.cloudfunctions.net",
    "steps_reboot": [
        {
            "name": "rebootProduction",
            "method": "POST",
            "url": "{{base_url}}/rebootProduction",
            "headers": {"Content-Type": "application/json"},
            "body": {"data": {"auth": "{{init_data}}", "session": "{{session_hash}}"}},
            "expect_status": [200],
            "extract": {
                "reboot_active": "result.active",
                "reboot_started_at": "result.startedAt",
                "reboot_ends_at": "result.endsAt",
            },
            "save": "reboot_raw",
        },
    ],
    "steps_scan": [
        {
            "name": "initUser",
            "method": "POST",
            "url": "{{base_url}}/initUser",
            "headers": {"Content-Type": "application/json"},
            "body": {"data": {"auth": "{{init_data}}", "session": "{{session_hash}}"}},
            "expect_status": [200],
            "extract": {
                "game_state": "result",
                "farm_ends_at": "result.passiveFarm.endsAt",
                "is_premium": "result.isPremium",
            },
            "save": "state_raw",
        },
    ],
}


def get_base_url(cfg: dict) -> str:
    """Базовый URL API: явный tma.base_url > пресет > origin webview."""
    tma_cfg = cfg.get("tma", {}) or {}
    url = (tma_cfg.get("base_url") or "").strip()
    if url:
        return url.rstrip("/")
    return FIREBASE_PRESET["base_url"].rstrip("/")


def get_steps(cfg: dict, kind: str) -> list:
    """Шаги для действия ('reboot'|'scan'): кастомные из конфига или пресет."""
    tma_cfg = cfg.get("tma", {}) or {}
    custom = tma_cfg.get(f"steps_{kind}") or []
    if custom:
        return custom
    return copy.deepcopy(FIREBASE_PRESET.get(f"steps_{kind}") or [])


# ---------------- шаблоны и JSON-пути ----------------

def render_template(value, ctx: dict):
    """Подставить {{переменные}} в строки (рекурсивно для dict/list)."""
    if isinstance(value, str):
        def repl(m):
            key = m.group(1).strip()
            v = ctx.get(key, "")
            return v if v is not None else ""
        return re.sub(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}", repl, value)
    if isinstance(value, dict):
        return {k: render_template(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render_template(v, ctx) for v in value]
    return value


def json_path(obj, path: str):
    """Простой JSON-путь через точку и [индекс]: 'data.user.name', 'items[0].id'."""
    if path in ("", "@", "$"):
        return obj
    cur = obj
    for token in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        token = token.strip()
        if token.startswith("[") and token.endswith("]"):
            idx = int(token[1:-1])
            if not isinstance(cur, list) or idx >= len(cur):
                return None
            cur = cur[idx]
        else:
            if not isinstance(cur, dict) or token not in cur:
                return None
            cur = cur[token]
    return cur


# ---------------- HTTP ----------------

def http_request(method: str, url: str, headers: dict = None, body=None, timeout: int = 25):
    """HTTP-запрос через urllib. Возвращает (status, headers, text).

    Ошибки сети поднимаются как TmaError; HTTP 4xx/5xx НЕ исключение —
    статус вернётся, решение принимает шаг (expect_status).
    """
    hdrs = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) Chrome/124.0 Mobile Safari/537.36"}
    if headers:
        hdrs.update({str(k): str(v) for k, v in headers.items()})
    data = None
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
        else:
            data = str(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=(method or "GET").upper())
    try:
        with urllib.request.urlopen(req, timeout=max(5, int(timeout))) as resp:
            raw = resp.read(4 * 1024 * 1024)
            return resp.status, dict(resp.headers), raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(4 * 1024 * 1024).decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        return e.code, dict(e.headers or {}), raw
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TmaError(f"Сеть недоступна: {e}")


# ---------------- конвейер шагов ----------------

class StepResult:
    def __init__(self, name, status, detail, response=None):
        self.name = name
        self.status = status  # ok | fail | skip
        self.detail = detail
        self.response = response  # распарсенный JSON или текст

    def to_dict(self):
        return {"name": self.name, "status": self.status, "detail": self.detail}


def run_steps(steps: list, ctx: dict, timeout: int = 25) -> dict:
    """Выполнить последовательность HTTP-шагов. Возвращает {ok, results, ctx}."""
    results = []
    ok = True
    for i, step in enumerate(steps or []):
        if not isinstance(step, dict) or not step.get("url"):
            results.append(StepResult(f"step{i}", "fail", "шаг без url").to_dict())
            ok = False
            break
        name = str(step.get("name") or f"step{i}")
        method = str(step.get("method") or "GET").upper()
        url = render_template(step.get("url"), ctx)
        headers = render_template(step.get("headers") or {}, ctx)
        body = render_template(step.get("body"), ctx) if step.get("body") is not None else None
        try:
            status, _, text = http_request(method, url, headers, body, timeout)
        except TmaError as e:
            results.append(StepResult(name, "fail", str(e)).to_dict())
            ok = False
            if (step.get("on_fail") or "abort") != "continue":
                break
            continue
        expect = step.get("expect_status") or [200]
        if not isinstance(expect, list):
            expect = [expect]
        if status not in [int(s) for s in expect if str(s).isdigit()] and status not in expect:
            results.append(StepResult(name, "fail", f"HTTP {status}: {text[:200]}").to_dict())
            ok = False
            if (step.get("on_fail") or "abort") != "continue":
                break
            continue
        # разбор ответа
        parsed = None
        try:
            parsed = json.loads(text) if text else None
        except ValueError:
            parsed = None
        # извлечение переменных
        extracted = {}
        for var, path in (step.get("extract") or {}).items():
            val = json_path(parsed, str(path)) if parsed is not None else None
            if val is None and text and path in ("", "@", "$"):
                val = text
            ctx[var] = val
            extracted[var] = val
        if step.get("save"):
            ctx[step["save"]] = text
        detail = f"HTTP {status}"
        if extracted:
            detail += f"; извлечено: {', '.join(extracted)}"
        results.append(StepResult(name, "ok", detail, parsed).to_dict())
    return {"ok": ok, "results": results, "ctx": ctx}


def build_context(cfg: dict, webview_url: str) -> dict:
    """Стартовый контекст для шагов.

    Переменные: init_data (сырая строка tgWebAppData), session_hash (поле hash
    из неё — игра шлёт его как session), base_url (tma.base_url > пресет > origin).
    """
    info = parse_webview_url(webview_url)
    base_url = (cfg.get("tma", {}).get("base_url") or "").strip()
    if not base_url:
        base_url = FIREBASE_PRESET["base_url"]
    session_hash = ""
    try:
        session_hash = dict(urllib.parse.parse_qsl(
            info["init_data"], keep_blank_values=True)).get("hash", "") or ""
    except ValueError:
        session_hash = ""
    return {
        "init_data": info["init_data"],
        "session_hash": session_hash,
        "webview_url": webview_url,
        "base_url": base_url.rstrip("/"),
        "origin": info["origin"],
    }


# ---------------- Discovery: поиск эндпоинтов в JS-бандле игры ----------------

_SCRIPT_RE = re.compile(r'<script[^>]+src=["\']([^"\']+)["\']', re.I)
_LINK_HREF_RE = re.compile(r'<link[^>]+href=["\']([^"\']+)["\']', re.I)
_API_PATH_RE = re.compile(r'["\'`](/(?:api|v\d|game|backend|server)[A-Za-z0-9_\-/.]{1,90})["\'`]')
_WSS_RE = re.compile(r'wss?://[A-Za-z0-9._\-:/?=&%]{3,120}')
_ABS_API_RE = re.compile(r'https?://[A-Za-z0-9.\-]+/[A-Za-z0-9_\-/.]*(?:api|auth|game|user|state|reboot|prod)[A-Za-z0-9_\-/.]{0,60}')

# Firebase-конфиг в JS-бандле: apiKey:"...",...,projectId:"..."
_FB_CFG_RE = re.compile(r'apiKey:"([A-Za-z0-9_\-]{20,60})"[^{}]{0,400}?projectId:"([a-z0-9\-]{4,40})"')
_FB_FUNCTIONS_HOST_RE = re.compile(
    r'https?://([a-z0-9\-]{4,20})-([a-z0-9\-]{4,40})\.cloudfunctions\.net')


def discover_endpoints(webview_url: str, timeout: int = 25, max_scripts: int = 12) -> dict:
    """Скачать страницу Mini App и её JS-бандлы, вытащить эндпоинты API.

    Возвращает отчёт-словарь (сохраняется в БД и показывается в веб-панели).
    """
    report = {"webview_url": webview_url, "origin": "", "page_fetched": False,
              "scripts": [], "endpoints": [], "absolute_urls": [], "websockets": [],
              "notes": []}
    status, headers, html = http_request("GET", webview_url, timeout=timeout)
    if status != 200:
        report["notes"].append(f"Страница игры вернула HTTP {status}")
        return report
    report["page_fetched"] = True
    final_url = headers.get("X-Self-URL") or webview_url
    info = parse_webview_url(final_url)
    report["origin"] = info["origin"] or parse_webview_url(webview_url)["origin"]

    srcs = _SCRIPT_RE.findall(html) + _LINK_HREF_RE.findall(html)
    urls = []
    for s in srcs:
        if s.startswith("http"):
            urls.append(s)
        elif s.startswith("/") and report["origin"]:
            urls.append(report["origin"] + s)
        elif report["origin"]:
            urls.append(report["origin"] + "/" + s.lstrip("./"))
    urls = list(dict.fromkeys(u.split("#")[0].split("?")[0] for u in urls if u))[:max_scripts]

    corpus = [html]
    for u in urls:
        try:
            st, _, text = http_request("GET", u, timeout=timeout)
            if st == 200 and text:
                corpus.append(text)
                report["scripts"].append({"url": u, "size": len(text)})
        except TmaError as e:
            report["notes"].append(f"{u}: {e}")

    blob = "\n".join(corpus)
    report["endpoints"] = sorted(set(_API_PATH_RE.findall(blob)))[:200]
    report["websockets"] = sorted(set(_WSS_RE.findall(blob)))[:20]
    report["absolute_urls"] = sorted(set(_ABS_API_RE.findall(blob)))[:100]

    # --- Firebase: конфиг в бандле + живая проверка callable-эндпоинта ---
    fb_projects = sorted(set(_FB_CFG_RE.findall(blob)))
    fb_hosts = _FB_FUNCTIONS_HOST_RE.findall(blob)
    candidates = []
    for region, project in fb_hosts:
        candidates.append((region, project))
    for _key, project in fb_projects:
        candidates.append(("us-central1", project))  # регион по умолчанию Firebase
    seen, api_base, api_check = set(), "", None
    for region, project in candidates:
        if (region, project) in seen or len(seen) >= 3:
            continue
        seen.add((region, project))
        base = f"https://{region}-{project}.cloudfunctions.net"
        try:
            status, _, text = http_request(
                "POST", base + "/syncTime",
                headers={"Content-Type": "application/json"}, body={"data": {}},
                timeout=timeout)
            parsed = _json_loads(text)
            if status == 200 and isinstance(parsed, dict) and "result" in parsed:
                api_base, api_check = base, {"probed": "/syncTime", "status": status,
                                            "response": text[:200]}
                break
        except TmaError:
            continue
    if api_base:
        report["api_base"] = api_base
        report["api_check"] = api_check
        report["notes"].append(
            f"Найден работающий API Firebase Cloud Functions: {api_base} "
            "(проверен синхронизацией времени /syncTime)")
    elif fb_projects:
        report["notes"].append(
            "Найден Firebase-конфиг (projectId: " + ", ".join(p for _k, p in fb_projects)
            + "), но callable-эндпоинт не ответил — возможен другой регион")

    if not report["endpoints"] and not report["absolute_urls"] and not api_base:
        report["notes"].append(
            "API-пути не найдены статически: возможно, приложение собирает запросы динамически "
            "или работает через WebSocket. Изучите список скриптов вручную."
        )
    return report


def _json_loads(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None
