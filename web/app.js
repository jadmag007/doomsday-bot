/* Doomsday Tyranny Bot — SPA (vanilla JS) */
"use strict";

/* ================= утилиты ================= */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function toast(msg, isErr = false) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("err", isErr);
  t.classList.remove("hidden");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hidden"), 3500);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) { showPin(); throw new Error("Требуется PIN"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || data.msg || ("HTTP " + res.status));
  return data;
}

const pad = (n) => String(n).padStart(2, "0");
function fmtDur(sec) {
  if (sec == null || isNaN(sec)) return "--:--:--";
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h > 99 ? `${h}ч ${pad(m)}м` : `${pad(h)}:${pad(m)}:${pad(s)}`;
}
function fmtTs(iso) {
  if (!iso) return "нет данных";
  return String(iso).replace("T", " ").slice(5, 16);
}
function fmtNum(v) {
  if (v == null) return "?";
  const f = Number(v);
  if (!isFinite(f)) return String(v);
  return f >= 1000 ? Math.round(f).toLocaleString("ru-RU") : String(Math.round(f * 10) / 10);
}

/* ================= PIN ================= */
function showPin() { $("pin-overlay").classList.remove("hidden"); }
async function tryPin() {
  const pin = $("pin-input").value;
  try {
    await api("/api/login", { method: "POST", body: { pin } });
    $("pin-overlay").classList.add("hidden");
    $("pin-error").textContent = "";
    boot();
  } catch (e) {
    $("pin-error").textContent = e.message;
  }
}
$("pin-btn").addEventListener("click", tryPin);
$("pin-input").addEventListener("keydown", e => { if (e.key === "Enter") tryPin(); });

/* ================= вкладки ================= */
document.querySelectorAll("#tabs .tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#tabs .tab").forEach(b => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".tab-page").forEach(p => p.classList.remove("active"));
    $("tab-" + btn.dataset.tab).classList.add("active");
    location.hash = btn.dataset.tab;
    if (btn.dataset.tab === "journal") loadJournal();
    if (btn.dataset.tab === "api") loadDiscovery();
  });
});
function openTab(name) {
  const btn = document.querySelector(`#tabs .tab[data-tab="${name}"]`);
  if (btn) btn.click();
}

/* ================= часы ================= */
setInterval(() => {
  const d = new Date();
  $("hdr-clock").textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}, 1000);

/* ================= обзор/дашборд ================= */
let OV = null;

async function refreshOverview() {
  try {
    OV = await api("/api/overview");
    renderOverview(OV);
  } catch (e) {
    if (!String(e.message).includes("PIN")) toast("Обзор: " + e.message, true);
  }
}

let WARN_PCT = 90;
function renderOverview(o) {
  $("hdr-version").textContent = "v" + o.version;
  const st = $("hdr-status");
  const running = o.running;
  if (running) {
    st.textContent = `⏳ ${running.action}…`;
    st.className = "hdr-status";
    const rb = $("running-banner");
    rb.classList.remove("hidden");
    rb.textContent = `Выполняется: ${running.action} (запуск #${running.id})`;
  } else {
    $("running-banner").classList.add("hidden");
    st.textContent = o.tma_configured ? "автоматизация вкл" : "TMA не настроен";
    st.className = "hdr-status " + (o.tma_configured ? "ok" : "err");
  }

  // таймеры
  const T = o.timers;
  $("t-reboot").textContent = fmtDur(T.reboot_in_sec);
  $("t-reboot-sub").textContent = T.last_reboot ? `последний: ${fmtTs(T.last_reboot)} · цикл ${T.reboot_interval_hours}ч` : "первый ребут при следующем проходе";
  $("t-scan").textContent = fmtDur(T.scan_in_sec);
  $("t-scan-sub").textContent = T.last_scan ? `последний: ${fmtTs(T.last_scan)} · каждые ${T.scan_interval_minutes}м` : "первый скан при следующем проходе";
  $("t-reboot-bar").style.width = pctOf(T.reboot_in_sec, T.reboot_interval_hours * 3600);
  $("t-scan-bar").style.width = pctOf(T.scan_in_sec, T.scan_interval_minutes * 60);
  // цикл производства (из API игры)
  const F = T.farm_cycle || {}, fEl = $("t-farm");
  fEl.style.color = "";
  if (!F.known) {
    fEl.textContent = "--:--:--";
    $("t-farm-sub").textContent = "нет данных (появится после скана/ребута)";
    $("t-farm-bar").style.width = "0%";
  } else if (F.active) {
    fEl.textContent = fmtDur(F.left_sec);
    $("t-farm-sub").textContent = `конец цикла: ${fmtTs(F.ends_at)}`;
    $("t-farm-bar").style.width = pctOf(F.left_sec, T.reboot_interval_hours * 3600);
  } else {
    fEl.textContent = "истёк";
    fEl.style.color = "var(--danger)";
    $("t-farm-sub").textContent = "требуется ребут — бот сделает его в ближайший проход";
    $("t-farm-bar").style.width = "100%";
  }

  // ресурсы
  const box = $("resources");
  const lastTs = (o.resources[0] || {}).ts;
  $("res-ts").textContent = lastTs ? `· ${fmtTs(lastTs)}` : "";
  if (!o.resources.length) {
    box.innerHTML = `<div class="muted">Нет данных. ${o.tma_configured ? "" : "Настройте TMA API (вкладка API / Discovery) — либо ждите уведомлений бота в чате."}</div>`;
  } else {
    box.innerHTML = o.resources.map(r => {
      const pct = r.pct != null ? Math.min(100, Math.max(0, r.pct)) : null;
      const cls = pct == null ? "" : pct >= 100 ? "full" : pct >= WARN_PCT ? "warn" : "";
      const stateBad = /переполн|full|ребут|перезагруз/i.test(r.state || "");
      return `<div class="res-row">
        <div class="res-name" title="${esc(r.name)}">${esc(r.name)}</div>
        <div class="res-bar"><div class="res-fill ${cls}" style="width:${pct == null ? 100 : pct}%; ${pct == null ? "opacity:.25" : ""}"></div></div>
        <div class="res-vals">${fmtNum(r.current)}/${fmtNum(r.max)}${r.state ? ` <span class="res-state ${stateBad ? "bad" : ""}">${esc(r.state)}</span>` : ""}</div>
      </div>`;
    }).join("");
  }

  renderEvents($("events"), o.events.slice(0, 8));
  renderTimersState(T, o);
}

function pctOf(leftSec, totalSec) {
  if (leftSec == null || !totalSec) return "0%";
  return Math.min(100, Math.max(0, (1 - leftSec / totalSec) * 100)).toFixed(1) + "%";
}

function renderEvents(el, events) {
  if (!el) return;
  if (!events.length) { el.innerHTML = `<div class="muted">Пока пусто</div>`; return; }
  el.innerHTML = events.map(e => `
    <div class="ev ${e.severity}">
      <div class="ev-time">${fmtTs(e.ts)}</div>
      <div>
        <div class="ev-title">${esc(e.title)}</div>
        ${e.body ? `<div class="ev-body">${esc(String(e.body).slice(0, 400))}</div>` : ""}
      </div>
    </div>`).join("");
}

/* тикеры обратного отсчёта */
setInterval(() => {
  if (!OV) return;
  const T = OV.timers;
  if (T.reboot_in_sec != null) $("t-reboot").textContent = fmtDur(T.reboot_in_sec - 1);
  if (T.scan_in_sec != null) $("t-scan").textContent = fmtDur(T.scan_in_sec - 1);
  const F = T.farm_cycle || {};
  if (F.left_sec != null) {
    const el = $("t-farm");
    if (F.active) el.textContent = fmtDur(F.left_sec - 1);
    else { el.textContent = "истёк"; el.style.color = "var(--danger)"; }
  }
}, 1000);

/* каждые 30 сек — обновление обзора; каждую секунду — локальные тикеры */
setInterval(refreshOverview, 30000);

/* ================= действия ================= */
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const action = btn.dataset.action;
  btn.disabled = true;
  try {
    const r = await api("/api/action", { method: "POST", body: { action } });
    toast(`Действие «${action}» запущено (#${r.run_id})`);
    setTimeout(refreshOverview, 1500);
    setTimeout(refreshOverview, 8000);
    if (action === "discover") setTimeout(loadDiscovery, 9000);
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
});

/* ================= таймеры: состояние ================= */
function renderTimersState(T, o) {
  const el = $("timers-state");
  if (!el) return;
  const F = T.farm_cycle || {};
  let farmLine;
  if (!F.known) farmLine = `<b>Цикл производства:</b> нет данных (после скана/ребута появится)`;
  else if (F.active) farmLine = `<b>Цикл производства:</b> активен, до ${fmtTs(F.ends_at)} (осталось ${fmtDur(F.left_sec)})`;
  else farmLine = `<b>Цикл производства:</b> <span style="color:var(--danger)">ИСТЁК — нужен ребут</span>`;
  el.innerHTML = `
    <div>${farmLine}</div>
    <div><b>Последний ребут:</b> ${fmtTs(T.last_reboot)} → следующий ${fmtTs(T.next_reboot)}</div>
    <div><b>Последний скан:</b> ${fmtTs(T.last_scan)} → следующий ${fmtTs(T.next_scan)}</div>
    <div><b>Сводка дня:</b> ${T.last_summary ? T.last_summary : "ещё не было"}</div>
    <div><b>API игры:</b> ${o.tma_preset ? "встроенный пресет (Firebase)" : "кастомные шаги"} · ${esc(o.tma_base_url || "?")}</div>`;
}

/* ================= загрузка журнала ================= */
let evOffset = 0;
async function loadJournal() {
  try {
    const kind = $("event-kind").value;
    const evs = await api(`/api/events?limit=30&offset=${evOffset}${kind ? "&kind=" + encodeURIComponent(kind) : ""}`);
    if (evOffset === 0) renderEvents($("journal-events"), evs);
    else $("journal-events").insertAdjacentHTML("beforeend", evs.map(e => evHtml(e)).join(""));
    const runs = await api("/api/runs");
    renderRuns(runs);
    const lg = await api("/api/log?lines=200");
    $("log-tail").textContent = (lg.lines || []).join("\n") || "— пусто —";
  } catch (e) { toast("Журнал: " + e.message, true); }
}
function evHtml(e) {
  return `<div class="ev ${e.severity}">
    <div class="ev-time">${fmtTs(e.ts)}</div>
    <div><div class="ev-title">${esc(e.title)}</div>
    ${e.body ? `<div class="ev-body">${esc(String(e.body).slice(0, 400))}</div>` : ""}</div>
  </div>`;
}
$("events-more").addEventListener("click", () => { evOffset += 30; loadJournal(); });
$("event-kind").addEventListener("change", () => { evOffset = 0; loadJournal(); });
$("log-refresh").addEventListener("click", loadJournal);

function renderRuns(runs) {
  const el = $("runs-list");
  if (!el) return;
  if (!runs.length) { el.innerHTML = `<div class="muted">Ручных запусков не было</div>`; return; }
  el.innerHTML = runs.map(r => `
    <div class="run">
      <div class="run-head">
        <div>#${r.id} · ${esc(r.action)} · ${fmtTs(r.started)}</div>
        <span class="badge ${r.status}">${r.status}</span>
      </div>
      ${r.log ? `<div class="run-log">${esc(r.log.slice(-1200))}</div>` : ""}
    </div>`).join("");
}

/* ================= старт ================= */
function boot() {
  refreshOverview();
  loadConfigIntoForms();
}
window.addEventListener("hashchange", () => {
  const h = location.hash.replace("#", "");
  if (h) openTab(h);
});
if (location.hash) openTab(location.hash.replace("#", ""));
boot();

/* ================================================================
   НАСТРОЙКИ: загрузка конфига в формы и обратно
================================================================ */
let CFG = null;
const NOTIFY_EVENTS = [
  ["resource_full", "Склад переполнен"],
  ["resource_warn", "Ресурс близок к максимуму"],
  ["reboot_report", "Отчёт о ребуте"],
  ["reboot_needed", "Цикл истёк — нужен ребут"],
  ["chat_alerts", "Уведомления бота в чате"],
  ["errors", "Ошибки"],
  ["daily_summary", "Сводка дня"],
  ["scan_report", "Отчёт каждого скана"],
];

async function loadConfigIntoForms() {
  try {
    CFG = await api("/api/config");
    WARN_PCT = (CFG.notify && CFG.notify.warn_threshold_pct) || 90;
    fillForms(CFG);
  } catch (e) { /* PIN-оверлей уже показан */ }
}

function fillForms(c) {
  const tg = c.telegram || {}, sc = c.schedules || {}, nf = c.notify || {},
        wb = c.web || {}, ch = c.chat || {}, tm = c.tma || {};
  // таймеры
  $("f-reboot-hours").value = sc.reboot_interval_hours ?? 12;
  $("f-scan-min").value = sc.scan_interval_minutes ?? 60;
  $("f-summary-time").value = sc.summary_time || "20:00";
  $("f-retry-min").value = sc.retry_failed_minutes ?? 20;
  // telegram
  $("f-api-id").value = tg.api_id || "";
  $("f-api-hash").value = tg.api_hash || "";
  $("f-api-hash").placeholder = c._has_api_hash ? "сохранён (маска)" : "вставьте api_hash";
  $("f-phone").value = tg.phone || "";
  $("f-game-bot").value = tg.game_bot || "";
  $("f-app-short").value = tg.app_short_name || "play";
  // чат
  $("f-read-last").value = ch.read_last_messages ?? 20;
  $("f-send-start").checked = !!ch.send_start_on_reboot;
  renderPatternRows($("chat-patterns"), (ch.parse_patterns || []), ["match", "title"], ["severity", "notify"]);
  // уведомления
  $("f-notify-enabled").checked = !!nf.enabled;
  $("f-open-game").checked = !!nf.open_game_button;
  $("f-warn-pct").value = nf.warn_threshold_pct ?? 90;
  const tg2 = $("notify-toggles");
  tg2.innerHTML = NOTIFY_EVENTS.map(([k, label]) =>
    `<label class="check"><input type="checkbox" data-nev="${k}" ${((nf.events || {})[k]) !== false ? "checked" : ""}> ${label}</label>`).join("");
  // веб
  $("f-web-host").value = wb.host || "127.0.0.1";
  $("f-web-port").value = wb.port ?? 8080;
  $("f-web-pin").value = "";
  $("f-web-pin").placeholder = wb.pin ? "задан (введите новый, чтобы сменить)" : "пусто = без PIN";
  // TMA
  $("f-tma-base").value = tm.base_url || "";
  $("f-tma-timeout").value = tm.timeout_seconds ?? 25;
  $("f-tma-enabled").checked = !!tm.enabled;
  renderStepRows($("steps-reboot"), tm.steps_reboot || []);
  renderStepRows($("steps-scan"), tm.steps_scan || []);
  const rs = tm.resources || {};
  $("f-res-jsonpath").value = rs.json_path || "";
  const f = rs.fields || {};
  $("f-f-name").value = f.name || "name";
  $("f-f-cur").value = f.current || "amount";
  $("f-f-max").value = f.max || "capacity";
  renderPatternRows($("res-patterns"), (rs.text_patterns || []), ["regex", "name"], ["enabled"]);
}

/* ---- редактор паттернов ---- */
function renderPatternRows(container, items, textFields, extraFields) {
  container.innerHTML = "";
  items.forEach((it, i) => container.appendChild(patternRow(it, i, textFields, extraFields)));
  container._items = items;
}
function patternRow(it, i, textFields, extraFields) {
  const div = document.createElement("div");
  div.className = "row";
  const fieldsHtml = textFields.map(f => `
    <div class="lbl">${f}</div>
    <input type="text" data-f="${f}" value="${esc(it[f] || "")}" ${f === "regex" || f === "match" ? 'style="font-family:var(--mono);font-size:.78rem"' : ""}>`).join("");
  const extraHtml = extraFields.map(f => {
    if (f === "notify") return `<label class="check lbl"><input type="checkbox" data-f="${f}" ${it[f] ? "checked" : ""}> push</label><div></div>`;
    if (f === "enabled") return `<label class="check lbl"><input type="checkbox" data-f="${f}" ${it[f] !== false ? "checked" : ""}> вкл</label><div></div>`;
    if (f === "severity")
      return `<div class="lbl">${f}</div><select data-f="${f}">${["info", "warn", "critical"].map(s =>
        `<option value="${s}" ${it[f] === s ? "selected" : ""}>${s}</option>`).join("")}</select>`;
    return "";
  }).join("");
  div.innerHTML = `
    <div class="row-head"><span class="row-title">#${i + 1}</span>
      <button class="row-del" title="Удалить">✕</button></div>
    <div class="row-grid">${fieldsHtml}${extraHtml}</div>`;
  div.querySelector(".row-del").addEventListener("click", () => div.remove());
  return div;
}
function collectRows(container, textFields, extraFields) {
  const out = [];
  container.querySelectorAll(":scope > .row").forEach(div => {
    const it = {};
    textFields.forEach(f => { const el = div.querySelector(`[data-f="${f}"]`); if (el) it[f] = el.value.trim(); });
    extraFields.forEach(f => {
      const el = div.querySelector(`[data-f="${f}"]`);
      if (!el) return;
      it[f] = el.type === "checkbox" ? el.checked : el.value;
    });
    if (Object.values(it).some(v => v !== "" && v !== false)) out.push(it);
  });
  return out;
}

/* ---- редактор HTTP-шагов ---- */
function renderStepRows(container, steps) {
  container.innerHTML = "";
  steps.forEach((s, i) => container.appendChild(stepRow(s, i)));
}
function stepRow(s, i) {
  const div = document.createElement("div");
  div.className = "row";
  const j = (v) => esc(v == null ? "" : (typeof v === "object" ? JSON.stringify(v, null, 1) : v));
  div.innerHTML = `
    <div class="row-head">
      <input class="row-title" data-f="name" value="${esc(s.name || "шаг " + (i + 1))}" style="width:45%">
      <div class="row-updown">
        <button class="btn small" data-mv="-1" title="Выше">↑</button>
        <button class="btn small" data-mv="1" title="Ниже">↓</button>
        <button class="row-del" title="Удалить">✕</button>
      </div>
    </div>
    <div class="row-grid full">
      <div class="lbl">метод</div>
      <select data-f="method">${["GET", "POST", "PUT", "PATCH", "DELETE"].map(m =>
        `<option ${((s.method || "GET").toUpperCase() === m) ? "selected" : ""}>${m}</option>`).join("")}</select>
      <div></div>
      <div class="lbl">url</div>
      <input type="text" data-f="url" value="${esc(s.url || "")}" placeholder="{{base_url}}/api/..." style="font-family:var(--mono);font-size:.78rem">
      <div></div>
      <div class="lbl">headers</div>
      <textarea data-f="headers" placeholder='{"Authorization": "tma {{init_data}}"}'>${j(s.headers)}</textarea>
      <div></div>
      <div class="lbl">body</div>
      <textarea data-f="body" placeholder='{"initData": "{{init_data}}"}'>${j(s.body)}</textarea>
      <div></div>
      <div class="lbl">extract</div>
      <textarea data-f="extract" placeholder='{"token": "token"}'>${j(s.extract)}</textarea>
      <div></div>
      <div class="lbl">save</div>
      <input type="text" data-f="save" value="${esc(s.save || "")}" placeholder="имя переменной для сырого ответа">
      <label class="check lbl" style="grid-column:1/-1"><input type="checkbox" data-f="on_fail_continue" ${(s.on_fail === "continue") ? "checked" : ""}> продолжать при ошибке</label>
    </div>`;
  div.querySelector(".row-del").addEventListener("click", () => div.remove());
  div.querySelectorAll("[data-mv]").forEach(b => b.addEventListener("click", () => {
    const dir = Number(b.dataset.mv);
    if (dir === -1 && div.previousElementSibling) div.parentNode.insertBefore(div, div.previousElementSibling);
    if (dir === 1 && div.nextElementSibling) div.parentNode.insertBefore(div.nextElementSibling, div);
    renumberSteps(div.parentNode);
  }));
  return div;
}
function renumberSteps(container) {
  container.querySelectorAll(":scope > .row").forEach((r, i) => {
    const n = r.querySelector('[data-f="name"]');
    if (n && /^шаг \d+$/.test(n.value)) n.value = "шаг " + (i + 1);
  });
}
function collectSteps(container) {
  const out = [];
  container.querySelectorAll(":scope > .row").forEach(div => {
    const g = (f) => { const el = div.querySelector(`[data-f="${f}"]`); return el ? el.value.trim() : ""; };
    const parseJson = (f) => {
      const raw = g(f);
      if (!raw) return undefined;
      try { return JSON.parse(raw); } catch { div.querySelector(`[data-f="${f}"]`).classList.add("json-invalid"); throw new Error("Некорректный JSON в " + f); }
    };
    const step = { name: g("name") || undefined, method: g("method") || "GET", url: g("url") };
    if (!step.url) return; // пропускаем пустые
    const h = parseJson("headers"); if (h !== undefined) step.headers = h;
    const b = parseJson("body"); if (b !== undefined) step.body = b;
    const ex = parseJson("extract"); if (ex !== undefined) step.extract = ex;
    if (g("save")) step.save = g("save");
    if (div.querySelector('[data-f="on_fail_continue"]').checked) step.on_fail = "continue";
    out.push(step);
  });
  return out;
}

$("add-step-reboot").addEventListener("click", () => {
  $("steps-reboot").appendChild(stepRow({ method: "POST", url: "{{base_url}}/" }, document.querySelectorAll("#steps-reboot .row").length));
});
$("add-step-scan").addEventListener("click", () => {
  $("steps-scan").appendChild(stepRow({ method: "GET", url: "{{base_url}}/" }, document.querySelectorAll("#steps-scan .row").length));
});
$("add-chat-pattern").addEventListener("click", () => {
  const items = $("chat-patterns")._items || [];
  $("chat-patterns").appendChild(patternRow({ match: "", title: "", severity: "warn", notify: true }, items.length, ["match", "title"], ["severity", "notify"]));
});
$("add-res-pattern").addEventListener("click", () => {
  $("res-patterns").appendChild(patternRow({ name: "", regex: "", enabled: true }, 0, ["regex", "name"], ["enabled"]));
});

/* ---- сохранение ---- */
function setMsg(id, text, isErr) {
  const el = $(id); el.textContent = text; el.style.color = isErr ? "var(--danger)" : "var(--accent2)";
  setTimeout(() => { el.textContent = ""; }, 4000);
}

$("save-timers").addEventListener("click", async () => {
  try {
    const c = await api("/api/config");
    c.schedules = {
      reboot_interval_hours: num($("f-reboot-hours").value, 12, 1, 168),
      scan_interval_minutes: num($("f-scan-min").value, 60, 4, 1440),
      summary_time: $("f-summary-time").value || "20:00",
      retry_failed_minutes: num($("f-retry-min").value, 20, 5, 720),
    };
    await api("/api/config", { method: "PUT", body: c });
    setMsg("timers-msg", "Сохранено ✔"); refreshOverview();
  } catch (e) { setMsg("timers-msg", e.message, true); }
});

function num(v, d, lo, hi) {
  const n = parseInt(v, 10);
  if (isNaN(n)) return d;
  return Math.min(hi, Math.max(lo, n));
}

$("save-settings").addEventListener("click", async () => {
  try {
    const c = await api("/api/config");
    c.telegram = {
      api_id: parseInt($("f-api-id").value, 10) || 0,
      api_hash: $("f-api-hash").value && !$("f-api-hash").value.includes("•") ? $("f-api-hash").value.trim() : c.telegram.api_hash,
      phone: $("f-phone").value.trim(),
      game_bot: ($("f-game-bot").value.trim() || "@DoomsDayTyrannybot"),
      session: c.telegram.session || "doomsday",
      app_short_name: $("f-app-short").value.trim() || "play",
    };
    c.chat = {
      read_last_messages: num($("f-read-last").value, 20, 0, 100),
      send_start_on_reboot: $("f-send-start").checked,
      parse_patterns: collectRows($("chat-patterns"), ["match", "title"], ["severity", "notify"]),
    };
    const events = {};
    document.querySelectorAll("[data-nev]").forEach(chk => { events[chk.dataset.nev] = chk.checked; });
    c.notify = {
      enabled: $("f-notify-enabled").checked,
      warn_threshold_pct: num($("f-warn-pct").value, 90, 50, 100),
      open_game_button: $("f-open-game").checked,
      events, priority_high: c.notify.priority_high || ["resource_full", "errors"],
    };
    c.web = {
      host: $("f-web-host").value.trim() || "127.0.0.1",
      port: num($("f-web-port").value, 8080, 1024, 65535),
      pin: $("f-web-pin").value.trim() || c.web.pin || "",
    };
    await api("/api/config", { method: "PUT", body: c });
    setMsg("settings-msg", "Сохранено ✔"); loadConfigIntoForms();
  } catch (e) { setMsg("settings-msg", e.message, true); }
});

$("save-api").addEventListener("click", async () => {
  try {
    const c = await api("/api/config");
    let stepsReboot, stepsScan;
    try { stepsReboot = collectSteps($("steps-reboot")); stepsScan = collectSteps($("steps-scan")); }
    catch (e) { setMsg("api-msg", e.message, true); return; }
    c.tma = {
      enabled: $("f-tma-enabled").checked,
      base_url: $("f-tma-base").value.trim(),
      timeout_seconds: num($("f-tma-timeout").value, 25, 5, 120),
      steps_reboot: stepsReboot,
      steps_scan: stepsScan,
      resources: {
        json_path: $("f-res-jsonpath").value.trim(),
        fields: { name: $("f-f-name").value.trim() || "name", current: $("f-f-cur").value.trim() || "amount", max: $("f-f-max").value.trim() || "capacity", state: (c.tma.resources || {}).fields?.state || "state" },
        text_patterns: collectRows($("res-patterns"), ["regex", "name"], ["enabled"]),
      },
    };
    await api("/api/config", { method: "PUT", body: c });
    setMsg("api-msg", "Сохранено ✔"); loadConfigIntoForms(); refreshOverview();
  } catch (e) { setMsg("api-msg", e.message, true); }
});

$("restart-web").addEventListener("click", async () => {
  try {
    await api("/api/restart-web", { method: "POST", body: {} });
    toast("Панель перезапускается — обновите страницу через пару секунд");
    setTimeout(() => location.reload(), 3000);
  } catch (e) { toast(e.message, true); }
});

/* ================= Discovery ================= */
async function loadDiscovery() {
  try {
    const d = await api("/api/discovery");
    renderDiscovery(d);
  } catch (e) { /* silent */ }
}
function renderDiscovery(d) {
  const el = $("discovery-report");
  if (!el) return;
  if (!d || !d.done) {
    el.innerHTML = `<div class="muted">Discovery ещё не запускался. Нажмите «Запустить Discovery» — отчёт появится здесь через ~10 секунд.</div>`;
    return;
  }
  const eps = d.endpoints || [], ws = d.websockets || [], abs = d.absolute_urls || [];
  el.innerHTML = `
    <div class="kv-list">
      <div><b>Mini App:</b> <span class="disc-list" style="display:inline">${esc(d.origin || "")}</span></div>
      <div><b>Найдено эндпоинтов:</b> ${eps.length}; <b>WebSocket:</b> ${ws.length}; <b>скриптов:</b> ${(d.scripts || []).length}</div>
    </div>
    ${eps.length ? `<div class="card-sub">Пути API (из JS-бандла игры):</div><ul class="disc-list">${eps.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    ${ws.length ? `<div class="card-sub">WebSocket:</div><ul class="disc-list">${ws.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    ${abs.length ? `<div class="card-sub">Абсолютные URL:</div><ul class="disc-list">${abs.slice(0, 30).map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    ${(d.notes || []).length ? `<div class="note">${d.notes.map(esc).join("<br>")}</div>` : ""}
    <div class="note">Обновите страницу панели или выполните doomsday discover в Termux, чтобы заново проверить API игры.</div>`;
}

