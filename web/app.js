/* Deep Researcher UI — vanilla JS, zero dipendenze.
 * Stile IDE scuro/ambra. Sezioni: icone, state, api, sse, activity feed,
 * banner azione, piano, statusbar, report, citations, history, settings,
 * theme, run lifecycle, init. */
"use strict";

/* ============================== icone ============================== */
const ICONS = {
  globe: "#i-globe", file: "#i-file", activity: "#i-activity",
  search: "#i-search", zap: "#i-zap", check: "#i-check", alert: "#i-alert",
  layers: "#i-layers", clock: "#i-clock", ban: "#i-ban", db: "#i-db",
  book: "#i-book", chevD: "#i-chev-d",
};

function icon(name, cls = "ic s12 ev-ic") {
  return `<svg class="${cls}" aria-hidden="true"><use href="${ICONS[name] || ICONS.activity}"/></svg>`;
}

/* ============================== state ============================== */
const state = {
  runId: null,
  status: "idle",          // idle|loading|running|completed|failed|cancelled
  startedAt: null,
  cycle: null,
  counters: { sources: 0, evidence: 0, queries: 0, claims: 0,
              tokens: 0, llm: 0 },
  engine: "...",
  settings: null,
  reportJson: null,        // cache per run (citazioni)
  eventSource: null,
  timer: null,
};

const $ = (id) => document.getElementById(id);

/* ============================== api ============================== */
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch { /* ignore */ }
    throw new Error(`${r.status}: ${detail}`);
  }
  return r;
}
const apiJson = (path, opts) => api(path, opts).then((r) => r.json());

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  setTimeout(() => { t.hidden = true; }, 3500);
}

/* ============================== sse ============================== */
function listenEvents(runId) {
  closeStream();
  const es = new EventSource(`/api/runs/${runId}/events`);
  state.eventSource = es;
  const types = [
    // eventi esistenti
    "replay_start", "run_started", "planning_started", "plan_generated",
    "cycle_started", "cycle_completed", "query_started",
    "search_result_found", "source_fetched", "source_failed",
    "source_evaluated", "source_rejected", "evidence_extracted",
    "gap_check_started", "gap_detected", "writing_started",
    "report_chunk", "verification_started",
    "claim_verified", "claim_corrected", "claim_removed", "claim_failed",
    "run_completed", "run_failed", "run_cancelled", "run_closed",
    // eventi nuovi lato backend
    "source_queued", "source_fetching", "source_reading", "usage_update",
  ];
  types.forEach((t) => es.addEventListener(t, (e) => {
    try { handleEvent(JSON.parse(e.data)); } catch (err) { console.error(err); }
  }));
  es.onerror = () => { /* transient: EventSource ritenta da solo */ };
}

function closeStream() {
  if (state.eventSource) { state.eventSource.close(); state.eventSource = null; }
}

/* ====================== feed attività (italiano) ====================== */
const CAT = {
  run_started: "success", run_completed: "success",
  planning_started: "info", plan_generated: "info",
  cycle_started: "info", cycle_completed: "info",
  gap_check_started: "info", verification_started: "info",
  run_closed: "info",
  query_started: "search", search_result_found: "search",
  source_queued: "source", source_fetching: "source", source_fetched: "source",
  source_reading: "source", source_failed: "source",
  source_evaluated: "decision", source_rejected: "warning",
  evidence_extracted: "source",
  gap_detected: "warning",
  report_chunk: "info", writing_started: "info",
  claim_verified: "success", claim_corrected: "warning",
  claim_removed: "warning", claim_failed: "error",
  run_failed: "error", run_cancelled: "warning",
  usage_update: null,           // gestito solo dai contatori
  replay_start: null,           // nessuna riga nel feed
};

/* evento -> frase breve in italiano */
function eventSentence(type, p = {}) {
  switch (type) {
    case "run_started":        return "Ricerca avviata";
    case "planning_started":   return "Pianificazione in corso";
    case "plan_generated":     return `Piano generato: ${(p.subquestions || []).length} sotto-domande, ${(p.queries || []).length} query`;
    case "cycle_started":      return "Ciclo avviato";
    case "query_started":      return `Cerco: avvio di ${p.count} query`;
    case "search_result_found": return `Trovati ${p.count} risultati`;
    case "source_queued":      return `In coda: ${shortUrl(p.url)}`;
    case "source_fetching":    return `Scarico pagina ${shortUrl(p.url)}`;
    case "source_fetched":     return `Letta pagina ${shortUrl(p.url)}`;
    case "source_reading":     return p.subquestion ? `Sto leggendo ${shortUrl(p.url)} per: ${p.subquestion}` : `Sto leggendo ${shortUrl(p.url)}`;
    case "source_failed":      return `Errore nel leggere ${shortUrl(p.url)} (${p.error || "errore"})`;
    case "source_evaluated":   return `Valutata fonte ${shortUrl(p.url || p.source_id)} (tipo ${p.source_type || "?"}, qualità ${p.quality ?? "?"})`;
    case "source_rejected":    return `Scartata fonte ${shortUrl(p.source_id || p.url)} — ${p.reason || "non rilevante"}`;
    case "evidence_extracted": return `Estratte ${p.count} evidenze (tot ${p.total})`;
    case "gap_check_started":  return "Controllo lacune informative";
    case "gap_detected":       return `Lacune rilevate: ${(p.missing || []).length} mancanti, ${(p.contradictions || []).length} contraddizioni, ${(p.new_queries || []).length} nuove query`;
    case "writing_started":    return "Scrittura del report";
    case "verification_started": return "Verifica dei claim in corso";
    case "claim_verified":     return `Verificato claim ${p.claim_id}`;
    case "claim_corrected":    return `Corretto claim ${p.claim_id} — ${p.reason || ""}`.trim();
    case "claim_removed":      return `Rimosso claim ${p.claim_id} — ${p.reason || ""}`.trim();
    case "claim_failed":       return `Verifica fallita claim ${p.claim_id} — ${p.reason || ""}`.trim();
    case "run_completed":      return "Ricerca completata";
    case "run_failed":         return `Ricerca fallita: ${p.error || "errore"}`;
    case "run_cancelled":      return "Ricerca interrotta";
    case "run_closed":         return "";  // non mostrato nel feed
    default:                   return "";
  }
}

function addActivity(type, cycle, payload) {
  const sentence = eventSentence(type, payload || {});
  if (!sentence) return;
  const li = document.createElement("li");
  li.className = `cat-${CAT[type] || "info"}`;
  const ic = icon(ICON_FOR[type] || "activity");
  const cyc = cycle ? `<span class="ev-cycle mono">c${cycle}</span>` : "";
  li.innerHTML = `${ic}${cyc}<span class="ev-text">${escapeHtml(sentence)}</span>`;
  const list = $("activity");
  list.prepend(li);
  while (list.children.length > 300) list.removeChild(list.lastChild);
  const fc = $("feed-count");
  if (fc) {
    const n = list.children.length;
    fc.textContent = `${n} eventi`;
  }
}

const ICON_FOR = {
  run_started: "zap", run_completed: "check", planning_started: "layers",
  plan_generated: "layers", cycle_started: "activity",
  cycle_completed: "activity", query_started: "search",
  search_result_found: "search", source_queued: "globe",
  source_fetching: "globe", source_fetched: "globe", source_reading: "book",
  source_failed: "alert", source_evaluated: "check", source_rejected: "ban",
  evidence_extracted: "file", gap_check_started: "activity",
  gap_detected: "alert", writing_started: "file", report_chunk: "file",
  verification_started: "check", claim_verified: "check",
  claim_corrected: "alert", claim_removed: "ban", claim_failed: "alert",
  run_failed: "alert", run_cancelled: "ban", run_closed: "activity",
};

function shortUrl(u) {
  if (!u) return "";
  return String(u).replace(/^https?:\/\//, "").replace(/\/$/, "").slice(0, 64);
}

/* ====================== banner azione corrente ====================== */
/* tipi che SETTANO un'azione attiva (dot ambra pulsante) */
const ACTION_ACTIVE = {
  query_started: (p) => `cerca: ${p.count || "?"} query in corso`,
  source_queued: (p) => `in coda: ${shortUrl(p.url)}`,
  source_fetching: (p) => `scaricando ${shortUrl(p.url)}`,
  source_reading: (p) => p.subquestion
    ? `leggendo ${shortUrl(p.url)} — sotto-domanda: ${p.subquestion}`
    : `leggendo ${shortUrl(p.url)}`,
  source_evaluated: (p) => `decidendo rilevanza: ${shortUrl(p.url || p.source_id)}`,
};
/* tipi che COMPLETANO un'azione (ultima azione in grigio) */
const ACTION_DONE = {
  search_result_found: (p) => `trovati ${p.count} risultati`,
  source_fetched: (p) => `letta ${shortUrl(p.url)}`,
  source_failed: (p) => `errore lettura ${shortUrl(p.url)}`,
  source_rejected: (p) => `scartata fonte ${shortUrl(p.source_id || p.url)}`,
  evidence_extracted: (p) => `estratte ${p.count} evidenze (tot ${p.total})`,
  gap_check_started: () => "controllo lacune",
  writing_started: () => "scrittura report",
  verification_started: () => "verifica claims",
  run_completed: () => "ricerca completata",
  run_failed: () => "ricerca fallita",
  run_cancelled: () => "ricerca interrotta",
};

function setAction(text, active) {
  const banner = $("action-banner");
  const txt = $("action-text");
  if (!text) text = "in attesa";
  txt.textContent = text;
  banner.classList.toggle("active", !!active);
  banner.classList.toggle("idle", !active);
}

function handleAction(type, p) {
  if (ACTION_ACTIVE[type]) { setAction(ACTION_ACTIVE[type](p), true); return; }
  if (ACTION_DONE[type]) { setAction(ACTION_DONE[type](p), false); }
}

/* ============================== piano ============================== */
function renderPlan(p) {
  const wrap = $("plan");
  $("plan-title").textContent = p.title || "";
  const subs = $("plan-subs");
  subs.innerHTML = (p.subquestions || []).map((s, i) =>
    `<li><span class="n">${String(i + 1).padStart(2, "0")}</span><span>${escapeHtml(s)}</span></li>`).join("");
  const qs = $("plan-queries");
  qs.innerHTML = (p.queries || []).map((q, i) =>
    `<li><span class="n">${String(i + 1).padStart(2, "0")}</span><span>${escapeHtml(q)}</span></li>`)
    .join("") || '<li class="faint">nessuna query</li>';
  wrap.hidden = false;
  wrap.classList.remove("collapsed");
}

function handleEvent(ev) {
  const { type, cycle, payload } = ev;
  const p = payload || {};
  addActivity(type, cycle, p);
  handleAction(type, p);

  if (type === "cycle_started") state.cycle = cycle;
  if (type === "plan_generated") renderPlan(p);
  if (type === "query_started") state.counters.queries += p.count || 0;
  if (type === "source_fetched") state.counters.sources += 1;
  if (type === "evidence_extracted") state.counters.evidence = p.total || 0;
  if (type === "usage_update") {
    state.counters.llm = p.llm_calls || 0;
    state.counters.tokens = (p.tokens_in || 0) + (p.tokens_out || 0);
    if (p.sources != null) state.counters.sources = p.sources;
    if (p.evidences != null) state.counters.evidence = p.evidences;
  }
  if (["claim_verified", "claim_corrected", "claim_removed", "claim_failed"]
    .includes(type)) state.counters.claims += 1;
  if (type === "run_started") setStatus("running");
  if (type === "report_chunk" && p.markdown) renderReport(p.markdown);
  if (type === "run_completed") finishRun("completed");
  if (type === "run_failed") finishRun("failed");
  if (type === "run_cancelled") finishRun("cancelled");
  if (type === "run_closed") { closeStream(); refreshHistory(); }
  updateStatusbar();
}

/* ============================== statusbar ============================== */
function setStatus(s) {
  state.status = s;
  const el = $("sb-state");
  el.textContent = s;
  el.className = `badge ${s}`;
  const dot = $("pill-dot");
  dot.className = `dot ${s}`;
  $("pill-state").textContent = s;
  const running = s === "running";
  $("stop-btn").hidden = !running;
  $("start-btn").disabled = running;
}

function finishRun(s) {
  setStatus(s);
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
}

function updateStatusbar() {
  $("sb-cycle").textContent = state.cycle ?? "-";
  $("sb-sources").textContent = state.counters.sources;
  $("sb-evidence").textContent = state.counters.evidence;
  $("sb-queries").textContent = state.counters.queries;
  $("sb-claims").textContent = state.counters.claims;
  $("sb-tokens").textContent = state.counters.tokens.toLocaleString("it-IT");
  $("sb-llm").textContent = state.counters.llm;
  $("sb-engine").textContent = state.engine;
}

function tickTime() {
  if (!state.startedAt) return;
  const s = Math.floor((Date.now() - state.startedAt) / 1000);
  $("sb-time").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/* ============================== report ============================== */
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderInline(text) {
  let s = escapeHtml(text);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\[(\d+)\]/g, '<button class="citation" data-n="$1">[$1]</button>');
  s = s.replace(/(https?:\/\/[^\s<)]+)/g,
    '<a href="$1" target="_blank" rel="noopener">$1</a>');
  return s;
}

function renderReport(markdown) {
  const out = [];
  for (const line of markdown.split("\n")) {
    const t = line.trim();
    if (!t) { out.push('<div class="sp"></div>'); continue; }
    const h = t.match(/^(#{1,3})\s+(.*)$/);
    if (h) {
      out.push(`<h${h[1].length + 1}>${renderInline(h[2])}</h${h[1].length + 1}>`);
    } else if (/^[-*]\s+/.test(t) || /^\d+\.\s+/.test(t)) {
      out.push(`<div class="li">${renderInline(t.replace(/^([-*]|\d+\.)\s+/, ""))}</div>`);
    } else {
      out.push(`<p>${renderInline(t)}</p>`);
    }
  }
  $("report").innerHTML = out.join("");
}

/* ====================== popover citazioni ====================== */
async function openCitation(n) {
  const data = await loadReportJson();
  const pop = $("popover");
  const body = $("popover-body");
  const claims = (data.claims || []).filter(
    (c) => Number(c.citation_no) === Number(n));
  if (!claims.length) {
    body.innerHTML = `<p class="muted">Nessun claim con citazione [${n}].</p>`;
  } else {
    const parts = [];
    for (const c of claims) {
      const evs = (data.evidences || []).filter(
        (e) => (c.evidence_ids || []).includes(e.evidence_id));
      const ver = (data.verifications || []).filter(
        (v) => v.claim_id === c.id);
      parts.push(`<p><b>Claim:</b> ${escapeHtml(c.text)}</p>`);
      for (const e of evs) {
        const src = (data.sources || []).find((s) => s.id === e.source_id);
        if (src) {
          const url = src.url_final || src.url;
          parts.push(`<p><span class="src-title">Fonte:</span> ` +
            `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">` +
            `${escapeHtml(src.title || url)}</a><br>` +
            `<span class="muted">${escapeHtml(src.domain || "")}</span></p>`);
        }
        parts.push(`<blockquote>“${escapeHtml(e.quote)}</blockquote>`);
        parts.push('<div class="sp"></div>');
      }
      for (const v of ver) {
        parts.push(`<p><span class="verdict ${escapeHtml(v.verdict)}">` +
          `${escapeHtml(v.verdict)}</span> ` +
          `<span class="muted">${escapeHtml(v.reason || "")}</span></p>`);
      }
      parts.push('<div class="sp"></div>');
    }
    body.innerHTML = parts.join("");
  }
  $("popover-title").textContent = `Citazione [${n}]`;
  pop.hidden = false;
}

async function loadReportJson() {
  if (state.reportJson) return state.reportJson;
  state.reportJson = await apiJson(`/api/runs/${state.runId}/report.json`);
  return state.reportJson;
}

function closePopover() { $("popover").hidden = true; }

/* ============================== history ============================== */
async function refreshHistory() {
  const runs = await apiJson("/api/runs");
  const ul = $("history-list");
  ul.innerHTML = "";
  const n = Math.min(runs.length, 30);
  const hc = $("history-count");
  if (hc) hc.textContent = String(n);
  for (const r of runs.slice(0, 30)) {
    const li = document.createElement("li");
    if (r.id === state.runId) li.className = "active";
    const date = r.started_at
      ? new Date(r.started_at * 1000).toLocaleString("it-IT",
        { dateStyle: "short", timeStyle: "short" }) : "";
    li.innerHTML =
      `<div class="q">${escapeHtml(String(r.question).slice(0, 90))}</div>` +
      `<div class="meta"><span class="badge ${escapeHtml(r.status)}">` +
      `${escapeHtml(r.status)}</span><span>${escapeHtml(date)}</span>` +
      `<span class="actions">` +
      `<a href="/api/runs/${r.id}/report.md" download title="esporta markdown">` +
      `${icon("download", "ic s12")}</a>` +
      `<a href="/api/runs/${r.id}/report.json" download title="esporta json">` +
      `json</a>` +
      `<a href="/api/runs/${r.id}/report.pdf" title="apri pdf">` +
      `${icon("file", "ic s12")}</a>` +
      `<button data-del="${r.id}" aria-label="Elimina run">` +
      `${icon("trash", "ic s12")}</button>` +
      `</span></div>`;
    li.addEventListener("click", (e) => {
      if (e.target.closest("[data-del]")) return;
      loadRun(r.id);
    });
    li.querySelector("[data-del]").addEventListener("click",
      () => deleteRun(r.id));
    ul.appendChild(li);
  }
}

async function loadRun(runId) {
  if (state.status === "running") { toast("Interrompi il run corrente prima."); return; }
  resetPanels();
  state.runId = runId;
  state.reportJson = null;
  setStatus("loading");
  try {
    const detail = await apiJson(`/api/runs/${runId}`);
    const run = detail.run || {};
    if (detail.report) renderReport(detail.report.markdown);
    Object.assign(state.counters, {
      sources: (detail.counts || {}).sources || 0,
      evidence: (detail.counts || {}).evidences || 0,
      claims: (detail.counts || {}).claims || 0,
    });
    if (detail.usage) {
      state.counters.llm = detail.usage.llm_calls || 0;
      state.counters.tokens =
        (detail.usage.llm_tokens_in || 0) + (detail.usage.llm_tokens_out || 0);
    }
    setStatus(run.status || "completed");
    state.startedAt = run.started_at ? run.started_at * 1000 : null;
    tickTime();
    listenEvents(runId); // replay + chiusura
    updateStatusbar();
  } catch (e) {
    toast(`Errore caricamento: ${e.message}`);
    setStatus("idle");
  }
}

async function deleteRun(runId) {
  if (!confirm("Eliminare definitivamente questo run?")) return;
  try {
    await api(`/api/runs/${runId}`, { method: "DELETE" });
    if (state.runId === runId) resetPanels();
    await refreshHistory();
  } catch (e) { toast(`Eliminazione fallita: ${e.message}`); }
}

/* ============================== settings ============================== */
async function loadSettings() {
  try {
    state.settings = await apiJson("/api/settings");
    const s = state.settings;
    state.engine = s.decision_engine === "laya" && s.laya_available
      ? `laya (${s.decision_laya_decisions || "tutte"}) + llm fallback`
      : "llm";
    $("sb-engine").textContent = state.engine;
    const rows = [
      ["llm", `${s.llm_provider} / ${s.llm_model}`],
      ["llm base url", s.llm_base_url],
      ["api key", s.llm_api_key === "set" ? "impostata" : "assente (.env)"],
      ["search", s.search_provider],
      ["decision engine", s.decision_engine],
      ["cicli max", s.max_cycles],
      ["concorrenza", s.max_concurrency],
    ];
    $("settings-body").innerHTML = rows.map(([k, v]) =>
      `<dt>${k}</dt><dd>${escapeHtml(String(v))}</dd>`).join("");
    const badge = s.laya_available
      ? '<span class="laya-badge on">Laya disponibile</span>'
      : '<span class="laya-badge off">Laya non installato</span>';
    try {
      const specs = await apiJson("/api/decision-specs");
      const layaNames = (s.decision_laya_decisions || "")
        .split(",").map((x) => x.trim()).filter(Boolean);
      $("decision-specs").innerHTML = badge + specs.specs.map((sp) => {
        const eng = s.decision_engine === "laya" && layaNames.includes(sp.name)
          ? "laya" : "llm";
        return `<div class="spec"><span>${escapeHtml(sp.name)} ` +
          `<span class="muted">(${escapeHtml(sp.kind)})</span></span>` +
          `<span class="eng ${eng}">${eng}</span></div>`;
      }).join("");
    } catch { $("decision-specs").innerHTML = badge; }
  } catch (e) {
    $("sb-engine").textContent = "n/d";
  }
}

/* ============================== tema ============================== */
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  localStorage.setItem("dr_theme", t);
}
function initTheme() {
  applyTheme(localStorage.getItem("dr_theme") || "dark");
  $("theme-toggle").addEventListener("click", () =>
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
}

/* ============================== run lifecycle ============================== */
function resetPanels() {
  $("activity").innerHTML = "";
  $("feed-count").textContent = "0 eventi";
  $("report").innerHTML = '<p class="muted">In attesa del report...</p>';
  $("plan").hidden = true;
  closePopover();
  setAction("", false);
  state.cycle = null;
  state.counters = { sources: 0, evidence: 0, queries: 0, claims: 0,
                     tokens: 0, llm: 0 };
  state.reportJson = null;
  state.startedAt = Date.now();
  $("sb-time").textContent = "0:00";
  updateStatusbar();
}

async function startRun(e) {
  e.preventDefault();
  const question = $("question").value.trim();
  if (!question) return;
  const body = {
    question,
    depth: $("depth").value,
    language: $("language").value === "auto" ? null : $("language").value,
  };
  try {
    const resp = await apiJson("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    resetPanels();
    state.runId = resp.run_id;
    setStatus("running");
    if (state.timer) clearInterval(state.timer);
    state.timer = setInterval(tickTime, 1000);
    listenEvents(resp.run_id);
    refreshHistory();
  } catch (err) {
    toast(`Avvio fallito: ${err.message}`);
  }
}

async function stopRun() {
  if (!state.runId) return;
  try {
    await api(`/api/runs/${state.runId}/cancel`, { method: "POST" });
    $("stop-btn").disabled = true;
    toast("Interruzione richiesta...");
  } catch (e) { toast(`Cancel fallito: ${e.message}`); }
}

/* ============================== init ============================== */
function init() {
  initTheme();
  $("run-form").addEventListener("submit", startRun);
  $("stop-btn").addEventListener("click", stopRun);
  $("popover-close").addEventListener("click", closePopover);
  $("plan-head").addEventListener("click", () => {
    const plan = $("plan");
    plan.classList.toggle("collapsed");
    $("plan-head").setAttribute(
      "aria-expanded", plan.classList.contains("collapsed") ? "false" : "true");
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closePopover();
  });
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("button.citation");
    if (btn) { openCitation(btn.dataset.n).catch((x) => toast(x.message)); return; }
    if (!$("popover").hidden && !e.target.closest("#popover")) closePopover();
  });
  loadSettings();
  refreshHistory();
}

document.addEventListener("DOMContentLoaded", init);
