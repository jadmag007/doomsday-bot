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
function fmtClock(iso) {
  if (!iso) return "";
  return String(iso).replace("T", " ").slice(11, 16);
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
    armTick(OV);
  } catch (e) {
    if (!String(e.message).includes("PIN")) toast("Обзор: " + e.message, true);
  }
}

let WARN_PCT = 90;
function renderOverview(o) {
  $("hdr-version").textContent = "v" + o.version;
  renderStaleBanner(o);
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

  // таймеры (скан переехал в статусбар сверху)
  const T = o.timers;
  // цикл производства (из API игры) + время авто-ребута
  const F = T.farm_cycle || {}, fEl = $("t-farm");
  fEl.style.color = "";
  if (!F.known) {
    fEl.textContent = "--:--:--";
    $("t-farm-sub").textContent = "нет данных (появится после скана/ребута)";
    $("t-farm-bar").style.width = "0%";
  } else if (F.active) {
    fEl.textContent = fmtDur(F.left_sec);
    const jit = F.reboot_jitter_sec ? `±${Math.round(F.reboot_jitter_sec / 60)} м` : "";
    $("t-farm-sub").textContent = `конец цикла: ${fmtTs(F.ends_at)}`
      + (F.reboot_at ? ` · авто-ребут ~${fmtClock(F.reboot_at)}${jit ? " " + jit : ""}` : "");
    $("t-farm-bar").style.width = pctOf(F.left_sec, F.total_sec || 12 * 3600);
  } else {
    fEl.textContent = "истёк";
    fEl.style.color = "var(--danger)";
    $("t-farm-sub").textContent = "ребут в ближайший проход cron (до 10 минут)";
    $("t-farm-bar").style.width = "100%";
  }

  renderResourcesState(o);
  renderTrackedDash(o);
  renderExchange(o);
  renderStatusbar(o);
  renderEvents($("events"), o.events.slice(0, 8));
  renderExLast(o.exchange && o.exchange.last || [], $("dash-exchanges"));
  renderTimersState(T, o);
}

/* баннер устаревшего процесса панели: backend != версии кода на диске */
function renderStaleBanner(o) {
  const sb = $("stale-banner");
  if (!sb) return;
  const stale = o.version && (o.backend == null || o.backend !== o.version);
  if (stale) {
    $("stale-want").textContent = o.version;
    $("stale-have").textContent = o.backend || "старая версия";
    sb.classList.remove("hidden");
  } else {
    sb.classList.add("hidden");
  }
}

async function pollReload(timeoutMs = 30000) {
  const t0 = Date.now();
  const iv = setInterval(async () => {
    try {
      const o = await api("/api/overview");
      if (o.backend && o.backend === o.version) { clearInterval(iv); location.reload(); }
    } catch (e) { /* панель ещё перезапускается */ }
    if (Date.now() - t0 > timeoutMs) { clearInterval(iv); location.reload(); }
  }, 1500);
}

$("stale-restart").addEventListener("click", async () => {
  try {
    await api("/api/restart-web", { method: "POST", body: {} });
    toast("Перезапускаю панель — страница обновится сама…");
    pollReload();
  } catch (e) {
    toast("Не вышло: " + e.message + " — выполните в Termux: doomsday start", true);
  }
});

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

/* ================= ресурсы: состояние + выбор для уведомлений ================= */
let RES_FILTER = [];
let RES_CATALOG = null;

/* общая строка ресурса — с иконкой игры */
function resRow(r, opts = {}) {
  const pct = r.pct != null ? Math.min(100, Math.max(0, r.pct)) : null;
  const cls = pct == null ? "" : pct >= 100 ? "full" : pct >= WARN_PCT ? "warn" : "";
  const stateBad = /переполн|full|ребут|перезагруз/i.test(r.state || "");
  const ico = r.id
    ? `<img class="res-ico" src="assets/items/${esc(r.id)}.webp" alt="" loading="lazy">`
    : `<span class="res-ico res-ico-blank"></span>`;
  return `<div class="res-row${opts.dim ? " dim" : ""}">
    ${ico}
    <div class="res-name" title="${esc(r.name)}">${esc(r.name)}</div>
    <div class="res-bar"><div class="res-fill ${cls}" style="width:${pct == null ? 100 : pct}%; ${pct == null ? "opacity:.25" : ""}"></div></div>
    <div class="res-vals">${fmtNum(r.current)}/${fmtNum(r.max)}${r.state ? ` <span class="res-state ${stateBad ? "bad" : ""}">${esc(r.state)}</span>` : ""}</div>
  </div>`;
}

function renderResourcesState(o) {
  const box = $("resources");
  if (!box) return;
  const flt = new Set(o.resource_filter || RES_FILTER || []);
  const ddtTracked = new Set(SELL_DDT_RIDS().filter(rid => flt.has(rid)));  // они — в статусбаре
  const lastTs = (o.resources[0] || {}).ts;
  $("res-ts").textContent = lastTs ? `· ${fmtTs(lastTs)}` : "";
  const tracked = o.resources.filter(r => r.id && flt.has(r.id) && !ddtTracked.has(r.id));
  const rest = o.resources.filter(r => !(r.id && flt.has(r.id)));

  // верхняя карточка: только отслеживаемые
  const tl = $("tracked-list");
  if (tl) {
    const ddtHint = ddtTracked.size ? `Отслеживаемые DDT-ресурсы (${ddtTracked.size} шт.) показаны в статусбаре сверху.` : "";
    if (!flt.size) {
      tl.innerHTML = `<div class="tracked-hint">Выбор не задан — уведомления приходят по <b>всем</b> ${o.resources.length || 40}+ ресурсам.
        Отметьте нужные в блоке «Выбор ресурсов», чтобы получать пуш только по ним.</div>`;
    } else if (!tracked.length) {
      tl.innerHTML = `<div class="muted">${ddtHint || "Данных ещё нет — появится после первого скана."}</div>`;
    } else {
      tl.innerHTML = tracked.map(r => resRow(r)).join("")
        + (ddtHint ? `<div class="muted" style="margin-top:.35rem">${ddtHint}</div>` : "");
    }
    const tc = $("tracked-count");
    if (tc) tc.textContent = flt.size ? `· ${flt.size}` : "";
  }

  // нижняя карточка: остальные (по умолчанию свёрнуты)
  if (!o.resources.length) {
    box.innerHTML = `<div class="muted">Нет данных. ${o.tma_configured ? "" : "Настройте TMA API (вкладка API / Discovery) — либо ждите уведомлений бота в чате."}</div>`;
    return;
  }
  const tb = $("res-toggle-rest");
  if (tb) tb.textContent = `${box.classList.contains("collapsed") ? "Показать" : "Скрыть"} (${rest.length})`;
  box.innerHTML = rest.map(r => resRow(r)).join("");
}

/* мини-список отслеживаемых на дашборде (DDT-продающиеся — в статусбаре) */
function renderTrackedDash(o) {
  const card = $("dash-tracked-card"), box = $("dash-tracked");
  if (!card || !box) return;
  const flt = new Set(o.resource_filter || RES_FILTER || []);
  if (!flt.size) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const ddtTracked = new Set(SELL_DDT_RIDS().filter(rid => flt.has(rid)));
  const tracked = (o.resources || []).filter(r => r.id && flt.has(r.id) && !ddtTracked.has(r.id));
  $("dash-tracked-count").textContent = `· ${flt.size}`;
  box.innerHTML = tracked.length
    ? tracked.map(r => resRow(r)).join("")
    : `<div class="muted">Отслеживаются только DDT-ресурсы — их таймер в статусбаре сверху.</div>`;
}

/* ================= автообмен / продажа ================= */
let EX_DIRTY = false;  // есть несохранённые правки правил — не перерисовывать их

document.addEventListener("change", e => {
  if (e.target.closest("#ex-rules") || e.target.id === "f-ex-enabled") EX_DIRTY = true;
});
document.addEventListener("input", e => {
  if (e.target.closest("#ex-rules")) EX_DIRTY = true;
});

function fmtBytesRu(v) {
  if (v == null || !isFinite(v)) return "?";
  const units = ["Б", "КиБ", "МиБ", "ГиБ", "ТиБ"];
  let f = Number(v), i = 0;
  while (Math.abs(f) >= 1024 && i < units.length - 1) { f /= 1024; i++; }
  return (i === 0 ? Math.round(f).toLocaleString("ru-RU")
    : (Math.round(f * 100) / 100).toLocaleString("ru-RU")) + " " + units[i];
}

function fmtProceeds(s, count) {
  const total = Number(count) * s.price;
  if (!isFinite(total)) return "—";
  if (s.is_ddt) return "→ +" + (Math.round(total * 100) / 100).toLocaleString("ru-RU") + " DDT";
  return "→ +" + fmtBytesRu(total);
}

function renderExchange(o) {
  const ex = o.exchange;
  if (!ex) return;
  const stateEl = $("ex-state");
  if (stateEl) stateEl.textContent = ex.enabled ? "· вкл" : "· выключен";
  const b = ex.balances || {};
  const balEl = $("ex-balances");
  if (balEl) balEl.innerHTML = `
    <div class="ex-bal"><span>Данные для серверов</span><b>${esc(b.coin_ru || "нет данных")}</b></div>
    <div class="ex-bal"><span>DDT (премиум)</span><b>${esc(b.mcoin_ru || "нет данных")}</b></div>`;
  const en = $("f-ex-enabled");
  if (en) en.checked = !!ex.enabled;
  renderExRules(ex, o);
  renderExLast(ex.last || []);
}

function renderExRules(ex, o) {
  const box = $("ex-rules");
  if (!box) return;
  if (EX_DIRTY && box.children.length) return;  // не сносить правки пользователя
  const rules = {};
  (ex.rules || []).forEach(r => { rules[r.rid] = r; });
  const byRid = {};
  ((o && o.resources) || []).forEach(r => { if (r.id) byRid[r.id] = r; });
  box.innerHTML = (ex.sellable || []).map(s => {
    const rule = rules[s.id] || {};
    const mode = rule.mode || "off";
    const res = byRid[s.id];
    const store = res
      ? `${fmtNum(res.current)} / ${fmtNum(res.max)}${res.pct != null ? " (" + Math.round(res.pct) + "%)" : ""}`
      : "нет данных склада";
    return `<div class="ex-rule${mode === "off" ? " off" : ""}" data-rid="${esc(s.id)}">
      <img class="res-ico" src="assets/items/${esc(s.id)}.webp" alt="" loading="lazy">
      <div class="ex-rule-main">
        <div class="ex-rule-name">${esc(s.name)}</div>
        <div class="muted">${esc(store)} · ×1 = ${esc(s.unit_ru)}</div>
      </div>
      <div class="ex-rule-ctrl">
        <select data-ex="mode" class="select">
          <option value="off" ${mode === "off" ? "selected" : ""}>Не менять</option>
          <option value="cap" ${mode === "cap" ? "selected" : ""}>При заполнении</option>
          <option value="always" ${mode === "always" ? "selected" : ""}>Сразу</option>
        </select>
        <input data-ex="threshold" type="number" min="10" max="100" step="5"
               value="${rule.threshold_pct ?? 90}" title="Порог заполнения, %">
        <input data-ex="keep" type="number" min="0" step="1"
               value="${rule.keep ?? 0}" title="Оставить на складе" placeholder="оставить">
      </div>
      <div class="ex-rule-proc muted">${res ? fmtProceeds(s, res.current) : "—"}</div>
    </div>`;
  }).join("");
  box.querySelectorAll("[data-ex='mode']").forEach(sel =>
    sel.addEventListener("change", () => updateExRow(sel.closest(".ex-rule"))));
  box.querySelectorAll(".ex-rule").forEach(updateExRow);
}

function updateExRow(row) {
  const mode = row.querySelector("[data-ex='mode']").value;
  row.querySelector("[data-ex='threshold']").style.display = mode === "cap" ? "" : "none";
  row.querySelector("[data-ex='keep']").style.display = mode === "off" ? "none" : "";
  row.classList.toggle("off", mode === "off");
}

function renderExLast(items, el) {
  el = el || $("ex-last");
  if (!el) return;
  if (!items || !items.length) {
    el.innerHTML = `<div class="muted">Обменов ещё не было.</div>`;
    return;
  }
  el.innerHTML = items.map(e => `
    <div class="ev info">
      <div class="ev-time">${fmtTs(e.ts)}</div>
      <div><div class="ev-title">${esc(e.title)}</div>
      ${e.body ? `<div class="ev-body">${esc(String(e.body).slice(0, 220))}</div>` : ""}</div>
    </div>`).join("");
}

$("save-exchange").addEventListener("click", async () => {
  try {
    const c = await api("/api/config");
    const rules = [];
    document.querySelectorAll("#ex-rules .ex-rule").forEach(row => {
      const mode = row.querySelector("[data-ex='mode']").value;
      if (mode === "off") return;
      rules.push({
        rid: row.dataset.rid,
        mode,
        threshold_pct: num(row.querySelector("[data-ex='threshold']").value, 90, 10, 100),
        keep: num(row.querySelector("[data-ex='keep']").value, 0, 0, 1e9),
        min: 1,
        enabled: true,
      });
    });
    c.exchange = { ...(c.exchange || {}), enabled: $("f-ex-enabled").checked, rules };
    await api("/api/config", { method: "PUT", body: c });
    EX_DIRTY = false;
    setMsg("ex-msg", "Сохранено ✔ — обмен при следующем скане");
    refreshOverview();
  } catch (e) { setMsg("ex-msg", e.message, true); }
});

$("dash-to-res").addEventListener("click", () => openTab("res"));
$("dash-to-ex").addEventListener("click", () => { openTab("res"); document.getElementById("exchange-card").scrollIntoView({ behavior: "smooth", block: "start" }); });
$("res-toggle-rest").addEventListener("click", () => {
  const box = $("resources");
  box.classList.toggle("collapsed");
  $("res-toggle-rest").textContent =
    `${box.classList.contains("collapsed") ? "Показать" : "Скрыть"}`;
  if (OV) {
    const flt = new Set(OV.resource_filter || RES_FILTER || []);
    const rest = (OV.resources || []).filter(r => !(r.id && flt.has(r.id)));
    $("res-toggle-rest").textContent += ` (${rest.length})`;
  }
});

async function loadCatalog() {
  try {
    const c = await api("/api/config");  // не читаем CFG до инициализации (TDZ)
    RES_FILTER = (c.notify && c.notify.resource_filter) || [];
    const d = await api("/api/resources-catalog");
    RES_CATALOG = d.resources || [];
    renderCatalog();
  } catch (e) { /* PIN-оверлей уже показан */ }
}

function renderCatalog() {
  const box = $("res-catalog");
  if (!box || !RES_CATALOG) return;
  const flt = new Set(RES_FILTER || []);
  box.innerHTML = RES_CATALOG.map(r =>
    `<label class="check res-check" data-search="${esc((r.name + " " + r.id).toLowerCase())}">
      <input type="checkbox" data-rid="${esc(r.id)}" ${flt.has(r.id) ? "checked" : ""}>
      <img class="res-ico" src="assets/items/${esc(r.id)}.webp" alt="" loading="lazy">
      <span class="res-check-name">${esc(r.name)}</span></label>`).join("");
  box.querySelectorAll("input[data-rid]").forEach(ch => ch.addEventListener("change", updateResCount));
  updateResCount();
}

/* поиск по каталогу — без перерисовки (чекбоксы сохраняют состояние) */
$("res-search").addEventListener("input", () => {
  const q = $("res-search").value.trim().toLowerCase();
  document.querySelectorAll("#res-catalog .res-check").forEach(lb => {
    lb.style.display = !q || lb.dataset.search.includes(q) ? "" : "none";
  });
});

function updateResCount() {
  const el = $("res-filter-count");
  if (!el) return;
  const n = document.querySelectorAll("#res-catalog input[data-rid]:checked").length;
  el.textContent = n ? `· выбрано ${n}` : "· выбрано 0 (уведомления по всем)";
}

$("res-select-all").addEventListener("click", () => {
  document.querySelectorAll("#res-catalog input[data-rid]").forEach(ch => { ch.checked = true; });
  updateResCount();
});
$("res-select-none").addEventListener("click", () => {
  document.querySelectorAll("#res-catalog input[data-rid]").forEach(ch => { ch.checked = false; });
  updateResCount();
});

$("save-res-filter").addEventListener("click", async () => {
  try {
    const c = await api("/api/config");
    const ids = [...document.querySelectorAll("#res-catalog input[data-rid]:checked")].map(ch => ch.dataset.rid);
    c.notify = { ...(c.notify || {}) };
    c.notify.resource_filter = ids;
    await api("/api/config", { method: "PUT", body: c });
    RES_FILTER = ids;
    setMsg("res-msg", "Сохранено ✔");
    if (OV) renderResourcesState(OV);
  } catch (e) { setMsg("res-msg", e.message, true); }
});

/* ================= статусбар (верх панели) ================= */
const SELL_DDT_RIDS = () => ((OV && OV.exchange && OV.exchange.sellable) || [])
  .filter(s => s.is_ddt).map(s => s.id);

function fmtShort(sec) {
  if (sec == null || isNaN(sec)) return "--:--";
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h > 99) return `${h}ч`;
  return h > 0 ? `${h}ч ${pad(m)}м` : `${pad(m)}:${pad(s)}`;
}

function renderStatusbar(o) {
  const T = o.timers || {};
  // скан-таймер (+ здоровье cron: проходы живы?)
  const scanChip = $("sb-scan");
  if (scanChip) {
    $("sb-scan-t").textContent = fmtShort(T.scan_in_sec);
    const cronOk = T.cron_alive !== false;  // нет данных — не пугаем
    scanChip.classList.toggle("stale", !cronOk);
    scanChip.title = cronOk
      ? `До следующего скана · последний проход cron: ${fmtTs(T.last_cron_ts) || "?"}`
      : `Проходы cron не видны больше 25 минут — автоматизация стоит! ` +
        `Проверьте: doomsday status, затем doomsday start`;
  }
  // ETA DDT-продающихся ресурсов (только отслеживаемые — они живут здесь, а не в списке)
  const ddtChip = $("sb-ddt");
  if (ddtChip) {
    const flt = new Set(o.resource_filter || RES_FILTER || []);
    const eta = o.ddt_eta || {};
    const rids = SELL_DDT_RIDS().filter(rid => flt.has(rid));
    const byRid = {};
    (o.resources || []).forEach(r => { if (r.id) byRid[r.id] = r; });
    const sell = {};
    ((o.exchange && o.exchange.sellable) || []).forEach(s => { sell[s.id] = s; });
    if (!rids.length) {
      ddtChip.classList.add("hidden");
    } else {
      ddtChip.classList.remove("hidden");
      const rid = rids[0];  // основной — первый отслеживаемый DDT-ресурс
      const it = eta[rid] || {};
      const res = byRid[rid];
      $("sb-ddt-img").src = `assets/items/${esc(rid)}.webp`;
      $("sb-ddt-name").textContent = rid === "uran_pills" ? "UO2" : (sell[rid] ? sell[rid].name : rid);
      $("sb-ddt-eta").textContent = it.eta_sec != null ? fmtShort(it.eta_sec)
        : (it.note || "—");
      $("sb-ddt-count").textContent = res && res.max != null
        ? `${fmtNum(res.current)}/${fmtNum(res.max)}` : "";
      const jit = (it.eta_sec != null && o.security && o.security.jitter_enabled) ? " ~" : "";
      ddtChip.title = rid === "uran_pills"
        ? `До следующей урановой таблетки (840 урана на штуку, 1 ч производство)${jit} — расчёт по доходу урана`
        : `До следующей единицы: ${sell[rid] ? sell[rid].name : rid}`;
    }
  }
  // ночной режим
  const nightChip = $("sb-night");
  if (nightChip) {
    const sec = o.security || {};
    if (sec.night_active) {
      nightChip.classList.remove("hidden");
      $("sb-night-t").textContent = `ночь до ${sec.night_to || "?"}`;
    } else {
      nightChip.classList.add("hidden");
    }
  }
}

/* тикеры: локальный отсчёт с моментального снимка OV (секунды реально бегут) */
let TICK = null;
function armTick(o) {
  const T = o.timers || {};
  const F = T.farm_cycle || {};
  const eta = (o.ddt_eta || {});
  const flt = new Set(o.resource_filter || RES_FILTER || []);
  let ddtEta = null;
  for (const rid of SELL_DDT_RIDS()) {
    if (flt.has(rid) && eta[rid] && eta[rid].eta_sec != null) { ddtEta = eta[rid].eta_sec; break; }
  }
  TICK = {
    at: Date.now(),
    scan: T.scan_in_sec,
    farm: F.active ? F.left_sec : null,
    ddt: ddtEta,
  };
}

setInterval(() => {
  if (!TICK) return;
  const el = Math.floor((Date.now() - TICK.at) / 1000);
  if (TICK.scan != null) { const e = $("sb-scan-t"); if (e) e.textContent = fmtShort(TICK.scan - el); }
  if (TICK.farm != null) {
    const e = $("t-farm");
    if (e && e.textContent !== "истёк") e.textContent = fmtDur(TICK.farm - el);
  }
  if (TICK.ddt != null) { const e = $("sb-ddt-eta"); if (e && e.textContent !== "—") e.textContent = fmtShort(TICK.ddt - el); }
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
  else farmLine = `<b>Цикл производства:</b> <span style="color:var(--danger)">ИСТЁК — ребут в ближайший проход</span>`;
  let rbLine = "";
  if (F.known && F.active && F.reboot_at) {
    const jit = F.reboot_jitter_sec ? ` ±${Math.round(F.reboot_jitter_sec / 60)} мин` : "";
    rbLine = `<div><b>Следующий авто-ребут:</b> ${fmtTs(F.reboot_at)}${jit} — за ${F.before_min ?? 10} мин до конца цикла, фарм не прерывается; проход cron дождётся точного момента</div>`;
  } else if (F.known && !F.active) {
    rbLine = `<div><b>Следующий авто-ребут:</b> ближайший проход cron (цикл уже истёк)</div>`;
  }
  const cronLine = T.last_cron_ts
    ? `<div><b>Последний проход cron:</b> ${fmtTs(T.last_cron_ts)} — ${T.cron_alive === false
        ? `<span style="color:var(--danger)">давно (автоматизация стоит?)</span>` : "норма"}</div>`
    : `<div><b>Последний проход cron:</b> нет данных</div>`;
  el.innerHTML = `
    <div>${farmLine}</div>
    ${rbLine}
    ${cronLine}
    <div><b>Последний ребут:</b> ${fmtTs(T.last_reboot)}</div>
    <div><b>Последний скан:</b> ${fmtTs(T.last_scan)} → следующий ${fmtTs(T.next_scan)}${(o.security && o.security.jitter_enabled) ? " (±разброс)" : ""}</div>
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
  loadCatalog();
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
  ["exchange_report", "Отчёты автообмена"],
  ["chat_alerts", "Уведомления бота в чате"],
  ["errors", "Ошибки"],
  ["daily_summary", "Сводка дня"],
  ["scan_report", "Отчёт каждого скана"],
];

async function loadConfigIntoForms() {
  try {
    CFG = await api("/api/config");
    WARN_PCT = (CFG.notify && CFG.notify.warn_threshold_pct) || 90;
    RES_FILTER = (CFG.notify && CFG.notify.resource_filter) || [];
    if (RES_CATALOG) renderCatalog();
    fillForms(CFG);
  } catch (e) { /* PIN-оверлей уже показан */ }
}

function fillForms(c) {
  const tg = c.telegram || {}, sc = c.schedules || {}, nf = c.notify || {},
        wb = c.web || {}, ch = c.chat || {}, tm = c.tma || {}, sec = c.security || {};
  // таймеры
  $("f-before-min").value = sc.reboot_before_end_minutes ?? 10;
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
  // безопасность
  const nm = sec.night_mode || {}, jt = sec.jitter || {};
  $("f-night-enabled").checked = !!nm.enabled;
  $("f-night-from").value = nm.from || "01:00";
  $("f-night-to").value = nm.to || "07:00";
  $("f-jitter-enabled").checked = jt.enabled !== false;
  $("f-jitter-scan").value = jt.scan_percent ?? 15;
  $("f-jitter-reboot").value = jt.reboot_seconds ?? 180;
  $("f-jitter-summary").value = jt.summary_minutes ?? 10;
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
      reboot_interval_hours: (c.schedules && c.schedules.reboot_interval_hours) ?? 12, // запасной интервал
      reboot_before_end_minutes: num($("f-before-min").value, 10, 0, 360),
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
    c.security = {
      night_mode: {
        enabled: $("f-night-enabled").checked,
        from: $("f-night-from").value || "01:00",
        to: $("f-night-to").value || "07:00",
      },
      jitter: {
        ...(c.security && c.security.jitter || {}),
        enabled: $("f-jitter-enabled").checked,
        scan_percent: num($("f-jitter-scan").value, 15, 0, 50),
        reboot_seconds: num($("f-jitter-reboot").value, 180, 0, 600),
        summary_minutes: num($("f-jitter-summary").value, 10, 0, 30),
      },
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
    toast("Панель перезапускается — страница обновится сама…");
    pollReload();
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

