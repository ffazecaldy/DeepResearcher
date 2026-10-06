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
  reading: 0,
  colSizes: { left: null, center: null, right: null },
  failedChecks: 0,
  lastSeq: null,
  runId: null,
  activeRunId: null,       // B1-fix2: run avviato da QUESTA pagina (in corso)
  observedOther: false,    // B1-fix2: stiamo vedendo un run diverso dall'attivo
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
  openStream(runId);
}

function openStream(runId) {
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
  /* B1: su errore persistente mostra banner; /api/health ogni 5s per ricollegare.
     B1-fix2: un singolo errore SSE NON e' "server giu'" (EventSource riconnette
     da solo): il banner parte solo dopo errori consecutivi, e la riconnessione
     NON fa piu' location.reload() durante un run (cancellava la vista live). */
  es.onerror = () => {
    _sseErrStreak += 1;
    if (_sseErrStreak >= 3 && state.status === "running") showServerBanner(true);
  };
  es.onopen = () => { _sseErrStreak = 0; };
}

/* B1: banner "Server non raggiungibile" + riconnessione automatica.
   B1-fix: il banner distingue "in avvio" (warm-up Laya) da "giu'" vero.
   B1-fix2: mai reload automatico durante un run — il poller health, quando il
   server torna, riacchiappa la vista live via SSE invece di ricaricare. */
let _healthTimer = null;
let _sseErrStreak = 0;
function showServerBanner(show, starting = false) {
  let b = $("server-banner");
  if (show && !b) {
    b = document.createElement("div");
    b.id = "server-banner";
    b.className = "server-banner";
    b.innerHTML = (starting
      ? "Server in avvio (caricamento modelli, ~30s)..."
      : "Server non raggiungibile.") +
      ' <button id="retry-btn" class="hbtn">Riprova</button>';
    document.body.prepend(b);
    $("retry-btn").addEventListener("click", () => location.reload());
    _healthTimer = setInterval(async () => {
      try {
        const r = await fetch("/api/health");
        if (r.ok) {
          if (_healthTimer) { clearInterval(_healthTimer); _healthTimer = null; }
          b.remove();
          // il run (se in corso) continua lato server: riprendiamo lo stream
          // senza reload, cosi' la vista non si azzera
          if (state.runId) { _sseErrStreak = 0; openStream(state.runId); }
        }
      } catch { /* ancora giu' */ }
    }, 5000);
  } else if (!show && b) {
    b.remove();
    if (_healthTimer) { clearInterval(_healthTimer); _healthTimer = null; }
  }
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
    case "planning_started":    return "Il bot sta analizzando la domanda e scegliendo le pagine";
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
    case "gap_check_started":   return "Il bot sta controllando cosa manca";
    case "gap_detected":       return `Lacune rilevate: ${(p.missing || []).length} mancanti, ${(p.contradictions || []).length} contraddizioni, ${(p.new_queries || []).length} nuove query`;
    case "writing_started":     return "Il bot sta preparando il report";
    case "verification_started": return "Il bot sta verificando ogni frase sulle fonti";
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
  // pagine: un solo messaggio per URL per ciclo (i cicli rivalgono fonti viste:
  // l'utente non deve vedere duplicati)
  if (["source_queued", "source_fetching", "source_fetched", "source_reading"]
      .includes(type)) {
    const key = `${cycle}|${type}|${payload.url || ""}`;
    if (!state.seenPageEvents) state.seenPageEvents = new Set();
    if (state.seenPageEvents.has(key)) return;
    state.seenPageEvents.add(key);
  }
  if (type === "cycle_started" && cycle === 1 && state.seenPageEvents) {
    state.seenPageEvents.clear();  // nuovo run, riparto pulito
  }
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
  planning_started: () => "il bot sta analizzando la domanda e scegliendo le pagine",
  gap_check_started: () => "il bot sta controllando cosa manca",
  writing_started: () => "il bot sta preparando il report",
  verification_started: () => "il bot sta verificando ogni frase sulle fonti",
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
  // B1: dedup eventi (replay + live possono sovrapporsi): id sequenziale per run
  if (ev.seq != null) {
    if (!state.lastSeq) state.lastSeq = new Map();
    const prev = state.lastSeq.get(state.runId) || 0;
    if (ev.seq <= prev && ev.type !== "replay_start") return;  // gia' visto
    state.lastSeq.set(state.runId, ev.seq);
  }
  addActivity(type, cycle, p);
  handleAction(type, p);
  updateCycleTabs(type, cycle);
  updateReadingIndicator(type);

  if (type === "cycle_started") state.cycle = cycle;
  if (type === "plan_generated") renderPlan(p);
  // B1: il contatore query arriva da UNA fonte (plan_generated) o da usage_update,
  // mai sommato da query_started (era la causa del "1038")
  if (type === "plan_generated") state.counters.queries = (p.queries || []).length;
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
  if (type === "run_completed") {
    // B1: "completata con N avvisi" quando ci sono verifiche fallite
    finishRun(state.failedChecks ? "completed-warnings" : "completed");
    // B1-fix2: il run attivo e' finito — torniamo in modalita' normale
    if (state.runId === state.activeRunId) state.activeRunId = null;
  }
  if (type === "claim_failed") {
    state.failedChecks = (state.failedChecks || 0) + 1;
  }
  if (type === "run_failed") finishRun("failed");
  if (type === "run_cancelled") finishRun("cancelled");
  if (type === "run_closed") { closeStream(); refreshHistory(); collapseActivity(); }
  updateStatusbar();
}

/* ── schede ciclo: una "scheda subagent" per ciclo richiesto ────────────── */
function expectedCycles() {
  const d = $("depth").value;
  return d === "rapida" ? 1 : d === "approfondita" ? 5 : 3;
}

function updateCycleTabs(type, cycle) {
  const wrap = $("cycle-tabs");
  if (!wrap) return;
  if (type === "run_started") {
    wrap.innerHTML = "";
    const n = expectedCycles();
    for (let i = 1; i <= n; i++) {
      const b = document.createElement("span");
      b.className = "ctab";
      b.id = `ctab-${i}`;
      b.innerHTML = '<span class="ct-dot"></span>' +
        `<span class="ct-spin"><svg class="ic s12 spin"><use href="#i-loader"/></svg></span>` +
        `<span>ciclo ${i}</span>`;
      wrap.appendChild(b);
    }
    return;
  }
  if (!cycle) return;
  const tab = document.getElementById(`ctab-${cycle}`);
  if (!tab) return;
  if (type === "cycle_started") tab.classList.add("working");
  if (type === "cycle_completed") { tab.classList.remove("working"); tab.classList.add("done"); }
  if (type === "run_completed" || type === "run_failed" || type === "run_cancelled") {
    wrap.querySelectorAll(".ctab.working").forEach((t) => {
      t.classList.remove("working");
      t.classList.add(type === "run_completed" ? "done" : "working");
    });
  }
}

/* ── spinner "leggendo…" (SOLO pagine in lettura, niente rumore) ────────── */
function updateReadingIndicator(type) {
  const ind = $("reading-indicator");
  if (!ind) return;
  if (type === "source_reading") {
    state.reading += 1;
  } else if (type === "evidence_extracted" || type === "source_failed"
             || type === "run_completed" || type === "run_failed"
             || type === "run_cancelled") {
    state.reading = 0;
  }
  ind.hidden = state.reading <= 0;
}

/* a fine run il feed si riduce: mostra solo gli ultimi 12 eventi */
function collapseActivity() {
  const list = $("activity");
  while (list.children.length > 12) list.removeChild(list.lastChild);
  const fc = $("feed-count");
  if (fc) fc.textContent = `${list.children.length} eventi`;
}

/* ============================== statusbar ============================== */
function setStatus(s) {
  state.status = s;
  const el = $("sb-state");
  if (s === "completed-warnings") {
    el.textContent = `completata con ${state.failedChecks} avvisi`;
    el.className = "badge completed-warnings";
  } else {
    el.textContent = s;
    el.className = `badge ${s}`;
  }
  const dot = $("pill-dot");
  dot.className = `dot ${s === "completed-warnings" ? "completed" : s}`;
  $("pill-state").textContent = el.textContent;
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
  const hint = $("popover-hint");
  if (hint) hint.hidden = true;
  if (data.notFound) {
    body.innerHTML = "<p class='dim'>Questo run è stato eliminato dalla " +
      "cronologia: il dettaglio citazioni non è più disponibile.</p>";
    $("popover-title").textContent = `Citazione [${n}]`;
    pop.hidden = false;
    return;
  }
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
          const ann = [];
          if (src.source_type) ann.push(`tipo: ${src.source_type}`);
          if (src.quality_score != null) {
            ann.push(`qualità: ${["bassa", "media", "alta"][src.quality_score] || src.quality_score}`);
          }
          parts.push(`<p><span class="src-title">Fonte:</span> ` +
            `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">` +
            `${escapeHtml(src.title || url)}</a><br>` +
            `<span class="muted">${escapeHtml(src.domain || "")}` +
            (ann.length ? ` — ${escapeHtml(ann.join(", "))}` : "") + `</span></p>`);
        }
        parts.push(`<blockquote>“${escapeHtml(e.quote)}</blockquote>`);
        parts.push('<div class="sp"></div>');
      }
      for (const v of ver) {
        parts.push(`<p><span class="verdict ${escapeHtml(v.verdict)}">` +
          `${escapeHtml(v.verdict)}</span> ` +
          `<span class="muted">${escapeHtml(v.reason || "")}</span></p>`);
        if (v.corrected_text) {
          parts.push(`<p><b>Testo corretto:</b> ${escapeHtml(v.corrected_text)}</p>`);
        }
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
  try {
    state.reportJson = await apiJson(`/api/runs/${state.runId}/report.json`);
  } catch (e) {
    if (String(e.message).startsWith("404")) {
      return { claims: [], evidences: [], sources: [], verifications: [],
               notFound: true };
    }
    throw e;
  }
  return state.reportJson;
}

function closePopover() {
  $("popover").hidden = true;
  const hint = $("popover-hint");
  if (hint) hint.hidden = false;
}

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

async function loadRun(runId, opts = {}) {
  /* B1-fix2: la cronologia e' cliccabile SEMPRE. Se un run e' in corso e si
     clicca un altro run, si entra in modalita' "osserva" (sola lettura): il
     run attivo continua lato server e si puo' tornare a vederlo dalla lista. */
  const viewingActive = state.runId === runId;
  if (state.status === "running" && !viewingActive && !opts.force) {
    toast("Run in corso: apri un altro run in osservazione (il run attivo continua).");
  }
  if (state.status === "running" && !viewingActive) {
    state.observedOther = true;   // stiamo guardando un run diverso da quello attivo
  } else {
    state.observedOther = false;
  }
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
    // B1-fix2: non toccare lo stato "running" del run attivo quando guardi un altro
    const isActiveRun = state.activeRunId && runId === state.activeRunId;
    setStatus(isActiveRun ? "running" : (run.status || "completed"));
    state.startedAt = run.started_at ? run.started_at * 1000 : null;
    tickTime();
    listenEvents(runId); // replay + chiusura
    updateStatusbar();
  } catch (e) {
    toast(`Errore caricamento: ${e.message}`);
    setStatus(state.activeRunId ? "running" : "idle");
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
  setReportEmpty();
  $("plan").hidden = true;
  closePopover();
  setAction("", false);
  state.reading = 0;
  state.failedChecks = 0;
  state.seenPageEvents = new Set();
  const ri = $("reading-indicator");
  if (ri) ri.hidden = true;
  const ct = $("cycle-tabs");
  if (ct) ct.innerHTML = "";
  state.cycle = null;
  state.counters = { sources: 0, evidence: 0, queries: 0, claims: 0,
                     tokens: 0, llm: 0 };
  state.reportJson = null;
  state.startedAt = Date.now();
  $("sb-time").textContent = "0:00";
  updateStatusbar();
}

function setReportEmpty() {
  $("report").innerHTML =
    '<div class="report-empty">' +
    '<svg class="ic s24"><use href="#i-file"/></svg>' +
    "<p>Il report apparira qui a fine ricerca,</p>" +
    '<p class="dim">costruito solo sulle evidenze verificate.</p></div>';
}

async function clearHistory() {
  if (!confirm("Svuotare DEFINITIVAMENTE tutta la cronologia?")) return;
  try {
    await api("/api/runs", { method: "DELETE" });
    resetPanels();
    setStatus("idle");
    state.runId = null;
    await refreshHistory();
    toast("Cronologia svuotata");
  } catch (e) { toast(`Svuotamento fallito: ${e.message}`); }
}

async function startRun(e) {
  e.preventDefault();
  const question = $("question").value.trim();
  if (!question) return;
  /* B1-fix: un solo run alla volta + server pronto (evita run duplicati
     cliccando Avvia durante il warm-up) */
  if (state.status === "running") { toast("Un run e' gia' in corso"); return; }
  const btn = $("start-btn");
  btn.disabled = true;
  try {
    const h = await fetch("/api/health");
    if (!h.ok) throw new Error("server non pronto");
  } catch {
    showServerBanner(true, true);
    btn.disabled = false;
    return;
  }
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
    state.activeRunId = resp.run_id;  // B1-fix2: run avviato qui = run attivo
    state.observedOther = false;
    setStatus("running");
    if (state.timer) clearInterval(state.timer);
    state.timer = setInterval(tickTime, 1000);
    listenEvents(resp.run_id);
    refreshHistory();
  } catch (err) {
    toast(`Avvio fallito: ${err.message}`);
  } finally {
    btn.disabled = false;
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
  $("history-clear").addEventListener("click", clearHistory);
  // header "Nuova ricerca" = reset visibile di modulo e pannelli
  const formReset = $("form-reset");
  formReset.addEventListener("click", () => {
    if (state.status === "running") { toast("Interrompi il run corrente prima."); return; }
    $("question").value = "";
    resetPanels();
    setStatus("idle");
    state.runId = null;
    $("question").focus();
  });
  formReset.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); formReset.click(); }
  });
  // resizer colonne: click = alterna larghezza larga/normale (limitata, salvata)
  const LIM = { left: [200, 420], center: [40, 70], right: [220, 460] };
  document.querySelectorAll(".rbtn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const col = btn.dataset.col;
      const root = document.documentElement.style;
      if (col === "center") {
        const cur = state.colSizes.center ?? 60;
        const next = cur >= 70 ? 40 : Math.min(70, cur + 15);
        state.colSizes.center = next;
        root.setProperty("--w-center", `${next}%`);
        document.querySelector(".col-center").classList.add("c-w");
        btn.classList.toggle("on", next !== 60);
      } else {
        const def = col === "left" ? 280 : 300;
        const cur = state.colSizes[col] ?? def;
        const [min, max] = LIM[col];
        const next = cur >= max ? min : Math.min(max, cur + 80);
        state.colSizes[col] = next;
        root.setProperty(`--w-${col}`, `${next}px`);
        btn.classList.toggle("on", next !== def);
      }
    });
  });
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
