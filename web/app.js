/* Deep Researcher UI — vanilla JS, zero dipendenze.
 * Sezioni: state, api, sse, activity, report, citations, history, settings, theme, init. */
"use strict";

/* ============================== state ============================== */
const state = {
  runId: null,
  status: "idle",       // idle|running|completed|failed|cancelled
  startedAt: null,
  cycle: null,
  counters: { sources: 0, evidence: 0, queries: 0, claims: 0 },
  engine: "…",
  settings: null,
  reportJson: null,     // cached per active run
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
    "replay_start", "run_started", "planning_started", "plan_generated",
    "cycle_started", "query_started", "search_result_found", "source_fetched",
    "source_failed", "source_evaluated", "source_rejected", "evidence_extracted",
    "gap_check_started", "gap_detected", "cycle_completed", "writing_started",
    "report_chunk", "verification_started", "claim_verified", "claim_corrected",
    "claim_removed", "claim_failed", "run_completed", "run_failed",
    "run_cancelled", "run_closed",
  ];
  types.forEach((t) => es.addEventListener(t, (e) => {
    try { handleEvent(JSON.parse(e.data)); } catch (err) { console.error(err); }
  }));
  es.onerror = () => { /* server close or transient: EventSource retries alone */ };
}

function closeStream() {
  if (state.eventSource) { state.eventSource.close(); state.eventSource = null; }
}

/* ============================== activity ============================== */
const CAT = {
  run_started: "success", run_completed: "success",
  planning_started: "info", plan_generated: "info",
  cycle_started: "info", cycle_completed: "info", query_started: "info",
  gap_check_started: "info", writing_started: "info",
  verification_started: "info",
  search_result_found: "source", source_fetched: "source",
  source_evaluated: "decision", source_rejected: "warning",
  source_failed: "error", evidence_extracted: "source",
  gap_detected: "warning", report_chunk: "info",
  claim_verified: "success", claim_corrected: "warning",
  claim_removed: "warning", claim_failed: "error",
  run_failed: "error", run_cancelled: "warning", run_closed: "info",
};

function addActivity(type, cycle, payload) {
  const li = document.createElement("li");
  li.className = `cat-${CAT[type] || "info"}`;
  const brief = briefPayload(type, payload);
  li.innerHTML = `<span class="ev-cycle">c${cycle ?? 0}</span>` +
    `<b>${escapeHtml(type)}</b>` +
    (brief ? ` — ${escapeHtml(brief)}` : "");
  const list = $("activity");
  list.prepend(li);
  while (list.children.length > 300) list.removeChild(list.lastChild);
}

function briefPayload(type, p = {}) {
  switch (type) {
    case "plan_generated":
      return `${(p.subquestions || []).length} sotto-domande, ` +
        `${(p.queries || []).length} query`;
    case "search_result_found": return `${p.count} risultati`;
    case "source_fetched": return shortUrl(p.url);
    case "source_failed": return `${shortUrl(p.url)} (${p.error || "?"})`;
    case "source_evaluated":
      return `${shortUrl(p.url || p.source_id)} tipo=${p.source_type || "?"} ` +
        `qualità=${p.quality ?? "?"}`;
    case "source_rejected": return `${p.source_id}: ${p.reason}`;
    case "evidence_extracted":
      return `+${p.count} (tot ${p.total})`;
    case "gap_detected":
      return `mancanti: ${(p.missing || []).length}, ` +
        `contraddizioni: ${(p.contradictions || []).length}, ` +
        `nuove query: ${(p.new_queries || []).length}`;
    case "claim_verified": return p.claim_id;
    case "claim_corrected": return `${p.claim_id}: ${p.reason || ""}`;
    case "claim_removed": return `${p.claim_id}: ${p.reason || ""}`;
    case "claim_failed": return `${p.claim_id}: ${p.reason || ""}`;
    case "run_failed": return p.error || "";
    case "report_chunk": return p.title || "";
    default: return "";
  }
}

function shortUrl(u) {
  if (!u) return "";
  return u.replace(/^https?:\/\//, "").slice(0, 70);
}

function handleEvent(ev) {
  const { type, cycle, payload } = ev;
  addActivity(type, cycle, payload);
  if (type === "cycle_started") { state.cycle = cycle; }
  if (type === "search_result_found") state.counters.queries += payload.count || 0;
  if (type === "source_fetched") state.counters.sources += 1;
  if (type === "evidence_extracted") state.counters.evidence = payload.total || 0;
  if (type === "claim_verified" || type === "claim_corrected" ||
      type === "claim_removed" || type === "claim_failed") {
    state.counters.claims += 1;
  }
  if (type === "run_started") setStatus("running");
  if (type === "report_chunk" && payload.markdown) renderReport(payload.markdown);
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
  const running = s === "running";
  $("stop-btn").hidden = !running;
  $("start-btn").disabled = running;
}

function finishRun(s) {
  setStatus(s);
  state.timer && clearInterval(state.timer);
}

function updateStatusbar() {
  $("sb-cycle").textContent = state.cycle ?? "–";
  $("sb-sources").textContent = state.counters.sources;
  $("sb-evidence").textContent = state.counters.evidence;
  $("sb-queries").textContent = state.counters.queries;
  $("sb-claims").textContent = state.counters.claims;
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

/* citation popover */
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
        parts.push(`<blockquote>“${escapeHtml(e.quote)}”</blockquote>`);
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
      `<a href="/api/runs/${r.id}/report.md" download title="markdown">md</a>` +
      `<a href="/api/runs/${r.id}/report.json" download title="json">json</a>` +
      `<a href="/api/runs/${r.id}/report.pdf" title="pdf">pdf</a>` +
      `<button data-del="${r.id}" aria-label="Elimina run">🗑</button>` +
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
    state.cycle = run.status === "completed" ? null : state.cycle;
    if (detail.report) renderReport(detail.report.markdown);
    Object.assign(state.counters, {
      sources: (detail.counts || {}).sources || 0,
      evidence: (detail.counts || {}).evidences || 0,
      claims: (detail.counts || {}).claims || 0,
    });
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
      ["api key", s.llm_api_key === "set" ? "● impostata" : "○ assente (.env)"],
      ["search", s.search_provider],
      ["decision engine", s.decision_engine],
      ["cicli max", s.max_cycles],
      ["concorrenza", s.max_concurrency],
    ];
    $("settings-body").innerHTML = rows.map(([k, v]) =>
      `<dt>${k}</dt><dd>${escapeHtml(String(v))}</dd>`).join("") +
      `<p class="muted small" style="grid-column:1/-1">Modificabili solo via file .env</p>`;
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
          `<span class="eng ${eng}">→ ${eng}</span></div>`;
      }).join("");
    } catch { $("decision-specs").innerHTML = badge; }
  } catch (e) {
    $("sb-engine").textContent = "n/d";
  }
}

/* ============================== theme ============================== */
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
  $("report").innerHTML = '<p class="muted">In attesa del report…</p>';
  closePopover();
  state.cycle = null;
  state.counters = { sources: 0, evidence: 0, queries: 0, claims: 0 };
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
    state.timer && clearInterval(state.timer);
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
    toast("Interruzione richiesta…");
  } catch (e) { toast(`Cancel fallito: ${e.message}`); }
}

/* ============================== init ============================== */
function init() {
  initTheme();
  $("run-form").addEventListener("submit", startRun);
  $("stop-btn").addEventListener("click", stopRun);
  $("popover-close").addEventListener("click", closePopover);
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
