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
    """Стартовый контекст для шагов."""
    info = parse_webview_url(webview_url)
    base_url = (cfg.get("tma", {}).get("base_url") or "").strip()
    if not base_url and info["origin"]:
        base_url = info["origin"]
    return {
        "init_data": info["init_data"],
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
    if not report["endpoints"] and not report["absolute_urls"]:
        report["notes"].append(
            "API-пути не найдены статически: возможно, приложение собирает запросы динамически "
            "или работает через WebSocket. Изучите список скриптов вручную."
        )
    return report
