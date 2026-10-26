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
  endedAt: null,           // fine run (live: Date.now(); storico: da DB)
  dbStatus: null,          // status del run letto dal DB (loadRun)
  loadedRun: null,         // {question, depth, language} per "Ripeti ricerca"
  warnings: null,          // {result:[], ops:[], total} da report.json
  warningsTotal: 0,
  stopReason: null,        // ragione di arresto (etichetta italiana)
  reportMarkdown: null,    // ultimo markdown renderizzato (copia/stampa/ricerca)
  reportView: "report",    // report | sources | verifs
  srcRows: null,           // righe vista fonti (con conteggi citazioni)
  lastCitationBtn: null,   // per Escape: focus all'ultimo bottone citazione
  verifsFilter: null,      // FIX 11: filtro vista Verifiche {kind,value}
  verifsData: null,        // report.json corrente (re-render con filtro)
  cycleStats: null,        // Map ciclo -> contributi (da eventi cycle_* del feed)
  reportScroll: new Map(), // FIX 9: runId -> scrollTop (ripristino solo stesso run)
  lastReportRun: null,     // ultimo run renderizzato (aggiornamenti in streaming)
};

const $ = (id) => document.getElementById(id);

/* ====================== etichette e formati (italiano) ====================== */
const STATUS_IT = {
  idle: "in attesa", loading: "caricamento…", running: "in corso",
  completed: "completata", failed: "fallita", cancelled: "interrotta",
};
/* codici stop_reason del backend -> etichetta breve */
const STOP_IT = {
  MAX_CICLI: "limite cicli",
  COPERTURA_COMPLETA: "copertura raggiunta",
  LIMITE_TEMPO: "limite tempo",
  LIMITE_PAGINE: "limite pagine",
  LIMITE_CICLO: "limite ciclo",
  ANNULLATO: "interrotta",
  NESSUNA_NOVITA: "nessuna novità",
  SOTTODOMANDE_NON_RISOLVIBILI: "sotto-domande non risolvibili",
};
/* stati di supporto [support:X] scritti dal verifier */
const SUPPORT_IT = {
  supported: "supportata", contradicted: "contraddetta",
  insufficient: "fonti insufficienti", mixed: "prove contrastanti",
};
const SUPPORT_HELP = {
  supported: "le fonti consultate riportano la stessa informazione",
  contradicted: "almeno una fonte riferisce il contrario",
  insufficient: "le fonti trovate non bastano a confermarla",
  mixed: "le fonti sono in disaccordo tra loro",
};
const FETCH_IT = {
  SUCCESS: "letta", PARTIAL_CONTENT: "letta parziale",
  FETCH_FAILED: "fetch fallito", EXTRACTION_FAILED: "estrazione fallita",
};

/* durata leggibile: "42 s" / "4 min 32 s" / "1 h 12 min"; null/NaN -> "—" */
function fmtDur(sec) {
  if (sec == null || !isFinite(sec) || sec < 0) return "—";
  sec = Math.round(sec);
  if (sec < 60) return `${sec} s`;
  const m = Math.floor(sec / 60);
  if (m < 60) { const rs = sec % 60; return rs ? `${m} min ${rs} s` : `${m} min`; }
  const h = Math.floor(m / 60), rm = m % 60;
  return rm ? `${h} h ${rm} min` : `${h} h`;
}
function statusLabel(s) {
  if (s === "completed-warnings") {
    return `completata con ${state.warningsTotal || 0} avvisi`;
  }
  return STATUS_IT[s] || s;
}
function stopTimer() {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
}
function startTimer() {
  stopTimer();
  state.timer = setInterval(tickTime, 1000);
}
function stripSupportPrefix(reason) {
  return String(reason || "").replace(/^\[support:\w+\]\s*/, "");
}

/* prefisso [cause:X] opzionale nel verdict_reason: causa strutturata del
   mancato supporto / della verifica non conclusiva. Se assente NON si
   inventa: la riga mostra solo il motivo testuale. */
const CAUSE_RE = /\[cause:(no_relevant_evidence|evidence_insufficient|doc_missing|truncated|overstated|contradiction|technical_error|invalid_output)\]/;
const CAUSE_IT = {
  no_relevant_evidence: "nessuna evidenza pertinente",
  evidence_insufficient: "evidenze insufficienti",
  doc_missing: "documento non disponibile",
  truncated: "fonte letta solo in parte",
  overstated: "affermazione più forte delle fonti",
  contradiction: "fonti in contraddizione",
  technical_error: "errore tecnico",
  invalid_output: "esito del verifier non valido",
};
const CAUSE_ACTION = {
  no_relevant_evidence: "cercare fonti più pertinenti: ripeti la ricerca con una domanda più mirata",
  evidence_insufficient: "ripeti la ricerca per raccogliere più fonti sull'argomento",
  doc_missing: "riprovare più tardi o aprire direttamente la fonte citata",
  truncated: "ripeti la ricerca: la pagina verrà riletta per intero",
  overstated: "confronta la frase con le fonti citate: va letta in modo più cauto",
  contradiction: "confronta le fonti citate: riportano affermazioni in conflitto",
  technical_error: "riprovare più tardi",
  invalid_output: "riprovare più tardi",
};
function parseCause(reason) {
  const m = CAUSE_RE.exec(String(reason || ""));
  return m ? m[1] : null;
}
/* il token [cause:X] è metadato, non testo: non va mostrato nel motivo */
function stripCauseToken(reason) {
  return String(reason || "")
    .replace(/\s*\[cause:\w+\]\s*/g, " ").replace(/\s+/g, " ").trim();
}

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
    // FIX 15: report_final e' persistito e replayato dal backend ma non era
    // in whitelist: il report finale (e la striscia verifiche) live arrivava
    // solo dopo reload/apertura da cronologia
    "report_final",
    // eventi nuovi lato backend
    "source_queued", "source_fetching", "source_reading", "usage_update",
    "source_duplicate", "source_reused", "cycle_contribution", "cycle_registry",
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
    case "source_fetched":     return p.reused
      ? `Riutilizzata pagina già nota ${shortUrl(p.url)}`
      : `Letta pagina ${shortUrl(p.url)}`;
    case "source_duplicate":   return `Duplicato scartato: ${shortUrl(p.url)} (stesso contenuto di ${shortUrl(p.duplicate_of || "altra fonte")})`;
    case "cycle_contribution": return `Ciclo ${p.cycle ?? ""}: +${p.new_evidences ?? 0} evidenze, +${p.new_domains ?? 0} domini nuovi`;
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
  // B1-fix3: prepend solo se l'utente è in cima (vista live); se ha scrollato
  // nel passato, l'evento si accoda senza strappare la posizione
  const pinnedTop = list.scrollTop <= 2;
  list.prepend(li);
  while (list.children.length > 300) list.removeChild(list.lastChild);
  if (!pinnedTop) list.scrollTop = list.scrollTop + li.offsetHeight;
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
  source_fetched: (p) => p.reused ? `riusata ${shortUrl(p.url)}` : `letta ${shortUrl(p.url)}`,
  source_duplicate: (p) => `duplicato: ${shortUrl(p.url)}`,
  cycle_contribution: (p) => `+${p.new_evidences || 0} evidenze, +${p.new_domains || 0} domini`,
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
  setPhase(ev.type);
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
  trackCycleEvent(type, cycle, p);
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
  if (type === "report_chunk" && p.markdown && p.phase === "draft") {
    // P0-1: bozza in streaming; il testo definitivo arriva con report_final
    renderReport(p.markdown + "\n\n*verifica in corso...*");
  }
  if (type === "report_final" && p.markdown) {
    renderReport(p.markdown);  // verified text replaces the draft
    // riga statistiche verifica (P0-1 payload: claims/verified/corrected/removed/failed/uncovered)
    if (p.verification) {
      renderVerifyStats(p.verification);
      // FIX 11: il contatore statusbar "claims estratti" deve mostrare gli
      // estratti reali (29), non gli eventi verifica visti in streaming
      if (p.verification.claims != null) state.counters.claims = p.verification.claims;
      // FIX 13: verifica conclusa con avvisi -> punto giallo nello stepper
      markVerificaDone((p.verification.failed || 0) > 0);
    }
    const cb = $("coherence-btn");
    if (cb) cb.hidden = false;
    initReportViews();  // tab Fonti/Verifiche ora hanno dati reali
  }
  if (type === "run_completed") {
    // B1: "completata con N avvisi" quando ci sono verifiche fallite
    state.warningsTotal = state.failedChecks;
    setStopReason(p.stop_reason, "completed");
    finishRun(state.failedChecks ? "completed-warnings" : "completed");
    // affina il conteggio avvisi (verifiche FAILED/WEAKENED + fonti fallite)
    refreshWarnings();
    // B1-fix2: il run attivo e' finito — torniamo in modalita' normale
    if (state.runId === state.activeRunId) state.activeRunId = null;
  }
  if (type === "claim_failed") {
    state.failedChecks = (state.failedChecks || 0) + 1;
  }
  if (type === "run_failed") {
    setStopReason(p.stop_reason, "failed");
    finishRun("failed");
  }
  if (type === "run_cancelled") {
    setStopReason(p.stop_reason || "ANNULLATO", "cancelled");
    finishRun("cancelled");
  }
  if (type === "run_closed") {
    // run morto lasciato "running" nel DB (crash del server): il replay finisce
    // con run_closed senza evento finale — correggiamo lo stato dal DB
    const st = ["completed", "failed", "cancelled", "completed-warnings"]
      .includes(state.status)
      ? state.status
      : (state.dbStatus && state.dbStatus !== "running" ? state.dbStatus : "completed");
    finishRun(st);
    // run morto lasciato "running" nel DB: ended_at non esiste, NON inventare
    // una durata (mostra "—")
    if (state.dbStatus === "running") { state.endedAt = null; tickTime(); }
    closeStream();
    refreshHistory();
    collapseActivity();
  }
  updateStatusbar();
}

/* ── schede ciclo: una "scheda subagent" per ciclo richiesto ────────────── */
function expectedCycles() {
  const d = $("depth").value;
  return d === "rapida" ? 1 : d === "approfondita" ? 8 : 3;
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

/* a fine run il feed si riduce a UNA RIGA nell'header ("Mostra attivita — N
   eventi"); nessun rettangolo vuoto e nessun evento cancellato: il click
   riapre il corpo COMPLETO e il conteggio descrive cio' che si rivedra' */
function collapseActivity() {
  setActivityCollapsed(true, false);
}

/* ============================== statusbar ============================== */
/* ── stepper fasi ────────────────────────────────────────────────────── */
const PHASES = ["piano", "ricerca", "lettura", "scrittura", "verifica"];
const PHASE_EVENTS = {
  planning_started: "piano", plan_generated: "piano",
  query_started: "ricerca", search_result_found: "ricerca",
  source_reading: "lettura", evidence_extracted: "lettura",
  writing_started: "scrittura", report_chunk: "scrittura",
  verification_started: "verifica", report_final: "verifica",
};
function setPhase(type) {
  const ph = PHASE_EVENTS[type];
  if (!ph) return;
  const idx = PHASES.indexOf(ph);
  document.querySelectorAll("#phase-stepper .step").forEach((el, i) => {
    el.classList.toggle("active", i === idx);
    el.classList.toggle("done", i < idx);
  });
}
function resetStepper() {
  document.querySelectorAll("#phase-stepper .step").forEach((el) => {
    el.classList.remove("active", "done");
  });
}

/* ── riga statistiche verifica ───────────────────────────────────────── */
/* FIX 11-12: etichette che dicono COSA contano. Le categorie misurano cose
   diverse e possono sovrapporsi: [support:X] = valutazione delle evidenze,
   verdict = esito della verifica; NON sono parti di uno stesso totale e non
   sono punteggi di verita'. Ogni numero cliccabile apre la vista Verifiche
   gia' filtrata per quello stato. */
function normText(s) {
  return String(s || "").toLowerCase().replace(/\s+/g, " ").trim();
}

/* riepilogo completo dalle verifiche di report.json (fonte unica live/storico) */
function summarizeVerifs(data) {
  const verifs = data.verifications || [];
  const byVerdict = {};
  const support = { supported: 0, contradicted: 0, insufficient: 0, mixed: 0 };
  for (const v of verifs) {
    byVerdict[v.verdict] = (byVerdict[v.verdict] || 0) + 1;
    const sup = parseSupport(v.reason).support;
    if (sup) support[sup] += 1;
  }
  return {
    claims: (data.claims || []).length,
    total: verifs.length,
    byVerdict,
    support,
    uncovered: 0,
  };
}

function chipHtml(cls, n, label, title, filter) {
  if (n == null) return "";
  const inner = `${n} ${label}`;
  if (!filter) {
    return `<span class="vs ${cls}" title="${escapeHtml(title)}">${inner}</span>`;
  }
  return `<button type="button" class="vs ${cls}" data-filter="${escapeHtml(filter)}" ` +
    `title="${escapeHtml(title)}">${inner}</button>`;
}

function renderVerifyStrip(sum) {
  const el = $("verify-stats");
  if (!el || !sum) return;
  const sup = sum.support;
  const byVerdict = sum.byVerdict || {};
  const bits = [];
  bits.push(chipHtml("cnt", sum.claims, "claims estratti",
    "Frasi (claim) estratte dal report finale: e' da queste che nascono le verifiche",
    "all"));
  bits.push(chipHtml("cnt", sum.total, "verifiche eseguite",
    "Verifiche registrate sul report (una per claim verificato)", "all"));
  if (sup) {
    bits.push(chipHtml("ok", sup.supported || 0, "supportate",
      "Claim con evidenze a supporto [support:supported]", "support:supported"));
    const unsup = (sup.contradicted || 0) + (sup.insufficient || 0) + (sup.mixed || 0);
    bits.push(chipHtml("bad", unsup, "non supportate",
      "Claim SENZA evidenze a supporto [support:X]: " +
      `${sup.contradicted || 0} contraddette, ${sup.insufficient || 0} fonti insufficienti, ` +
      `${sup.mixed || 0} contrastate`, "support:unsupported"));
    if ((sup.contradicted || 0) > 0 || (sup.insufficient || 0) > 0 || (sup.mixed || 0) > 0) {
      bits.push(chipHtml("bad", sup.contradicted || 0, "contraddette",
        "Un'evidenza riferisce il contrario [support:contradicted]", "support:contradicted"));
      bits.push(chipHtml("bad", sup.insufficient || 0, "fonti insufficienti",
        "Le fonti trovate non bastano a confermarla [support:insufficient]", "support:insufficient"));
      bits.push(chipHtml("bad", sup.mixed || 0, "contrastate",
        "Le fonti sono in disaccordo tra loro [support:mixed]", "support:mixed"));
    }
  }
  bits.push(chipHtml("fix", byVerdict.CORRECTED || 0, "corrette",
    "Claim corretti dal verifier (verdict CORRECTED): il testo corretto sostituisce l'originale nel report",
    "verdict:CORRECTED"));
  bits.push(chipHtml("rem", byVerdict.REMOVED || 0, "rimosse",
    "Claim rimossi dalla versione finale (verdict REMOVED): il motivo dell'esclusione e' indicato in ogni voce della vista Verifiche",
    "verdict:REMOVED"));
  bits.push(chipHtml("bad", byVerdict.FAILED || 0, "verifiche non conclusive",
    "Verifica non conclusa per errori tecnici (fallback/errori LLM, verdict FAILED); non include le fonti insufficienti. Per ognuna la vista Verifiche indica se la frase e' ancora nel report o gia' gestita",
    "verdict:FAILED"));
  if ((byVerdict.WEAKENED || 0) > 0) {
    bits.push(chipHtml("fix", byVerdict.WEAKENED, "indebolite",
      "Claim indeboliti (verdict WEAKENED): lasciati nel report ma segnalati",
      "verdict:WEAKENED"));
  }
  if (sum.uncovered) {
    bits.push(chipHtml("bad", sum.uncovered, "frasi non coperte",
      "Frasi fattuali non coperte da nessun claim verificato (controllo di copertura testo→claim)"));
  }
  el.innerHTML = bits.filter(Boolean).join("");
  el.hidden = false;
  el.querySelectorAll(".vs[data-filter]").forEach((b) =>
    b.addEventListener("click", () => setVerifsFilter(b.dataset.filter)));
  // FIX 12: run-header esplicito su cosa contiene il report
  const rc = $("rh-claims");
  if (rc) {
    rc.hidden = !sum.claims;
    rc.textContent = `claims estratti: ${sum.claims}`;
  }
}

/* versione provvisoria dal payload report_final (report.json arriva a seguire
   con il dettaglio [support:X] e rimpiazza la striscia con i dati completi) */
function renderVerifyStats(v) {
  if (!v) return;
  renderVerifyStrip({
    claims: v.claims ?? null,
    total: (v.verified || 0) + (v.corrected || 0) + (v.removed || 0) + (v.failed || 0),
    byVerdict: {
      VERIFIED: v.verified || 0, CORRECTED: v.corrected || 0,
      REMOVED: v.removed || 0, FAILED: v.failed || 0, WEAKENED: 0,
    },
    support: null,
    uncovered: v.uncovered || 0,
  });
}

/* FIX 13: fase Verifica conclusa CON AVVISI -> punto giallo invece del verde */
function markVerificaDone(warn) {
  const step = document.querySelector('#phase-stepper .step[data-phase="verifica"]');
  if (!step) return;
  step.classList.remove("active");
  step.classList.add("done");
  step.classList.toggle("warn", !!warn);
}

/* ── filtro vista Verifiche (FIX 11: ogni contatore apre qui gia' filtrato) ── */
const VERIFS_FILTER_DEFS = [
  { key: "all", label: "tutte" },
  { key: "support:supported", label: "supportate" },
  { key: "support:unsupported", label: "non supportate" },
  { key: "support:contradicted", label: "contraddette" },
  { key: "support:insufficient", label: "insufficienti" },
  { key: "support:mixed", label: "contrastate" },
  { key: "verdict:VERIFIED", label: "confermate" },
  { key: "verdict:CORRECTED", label: "corrette" },
  { key: "verdict:WEAKENED", label: "indebolite" },
  { key: "verdict:REMOVED", label: "rimosse" },
  { key: "verdict:FAILED", label: "non conclusive" },
];
function parseFilterKey(key) {
  const [kind, value] = String(key || "all").split(":");
  return { kind: value ? kind : "all", value: value || null };
}
function verifsMatch(v, f) {
  if (!f || f.kind === "all") return true;
  if (f.kind === "verdict") return v.verdict === f.value;
  if (f.kind === "support") {
    const { support } = parseSupport(v.reason);
    if (f.value === "unsupported") return !!support && support !== "supported";
    return support === f.value;
  }
  return true;
}
function setVerifsFilter(key) {
  state.verifsFilter = parseFilterKey(key);
  if (!state.verifsData) {
    toast("Nessuna verifica disponibile: il report non e' ancora pronto.");
    return;
  }
  setReportView("verifs");
  renderVerifsView(state.verifsData);
}

/* ── pannello coerenza ───────────────────────────────────────────────── */
async function runCoherence() {
  if (!state.runId) return;
  const panel = $("coherence-panel");
  if (!panel) return;
  panel.hidden = false;
  panel.className = "coherence c-warn";
  panel.innerHTML = '<div class="coherence-head"><span class="c-badge">controllo in corso…</span></div>';
  try {
    const r = await apiJson(`/api/runs/${state.runId}/coherence`);
    renderCoherence(r);
  } catch (err) {
    panel.innerHTML = `<div class="coherence-head"><span class="c-badge">errore</span></div>` +
      `<p class="dim">Controllo coerenza non disponibile: ${escapeHtml(err.message)}</p>`;
  }
}
function renderCoherence(r) {
  const panel = $("coherence-panel");
  if (!panel) return;
  panel.hidden = false;
  panel.className = r.passed ? "coherence c-ok" : "coherence c-warn";
  const TYPE_LABEL = {
    uncovered: "non coperta", unsupported: "non supportata",
    contradiction: "contraddetta", numero_non_supportato: "numero inventato",
    unknown_url: "URL ignoto",
  };
  const parts = [`<div class="coherence-head"><span class="c-badge">${
    r.passed ? "coerente" : `${r.issues.length} problemi`}</span>` +
    `<span class="cr">${r.checked_sentences} frasi analizzate</span></div>`];
  if (r.issues && r.issues.length) {
    parts.push("<ul>");
    for (const i of r.issues) {
      parts.push(`<li><b class="ct t-${escapeHtml(i.type)}">${escapeHtml(TYPE_LABEL[i.type] || i.type)}</b>` +
        `${escapeHtml((i.sentence || "").slice(0, 120))}` +
        `<span class="cr"> — ${escapeHtml((i.reason || "").slice(0, 140))}</span></li>`);
    }
    parts.push("</ul>");
  }
  panel.innerHTML = parts.join("");
}

/* ── filtro cronologia ───────────────────────────────────────────────── */
function applyHistoryFilter() {
  const q = ($("history-filter")?.value || "").toLowerCase().trim();
  document.querySelectorAll("#history-list li").forEach((li) => {
    li.hidden = q && !(li.dataset.q || "").includes(q);
  });
}

function setStatus(s) {
  state.status = s;
  const el = $("sb-state");
  el.textContent = statusLabel(s);
  el.className = `badge ${s}`;
  // il badge stato è cliccabile SOLO quando ci sono avvisi da consultare
  const clickable = s === "completed-warnings" && (state.warningsTotal || 0) > 0;
  el.disabled = !clickable;
  el.classList.toggle("clickable", clickable);
  el.title = clickable ? "Mostra il dettaglio degli avvisi" : "";
  const dot = $("pill-dot");
  dot.className = `dot ${s === "completed-warnings" ? "completed" : s}`;
  $("pill-state").textContent = el.textContent;
  const rs = $("rh-status");
  if (rs) { rs.textContent = statusLabel(s); rs.className = `badge ${s}`; }
  // Interrompi SOLO se il run visualizzato è davvero il nostro run in corso
  // (attivo qui, oppure osservato dopo reload: il DB lo segnala "running")
  const reallyRunning = s === "running" &&
    (state.runId === state.activeRunId || state.dbStatus === "running");
  $("stop-btn").hidden = !reallyRunning;
  $("start-btn").disabled = s === "running";
}

function finishRun(s) {
  if (state.status === "running" && !state.endedAt) state.endedAt = Date.now();
  stopTimer();
  setStatus(s);
  // B14: il marker "verifica in corso" della bozza non deve mai sopravvivere
  // alla fine del run (run vecchi senza report_final lo lasciavano stampato).
  // FIX 10: il marker ora e' RENDERIZZATO in corsivo (<em>), non asterischi.
  const rep = $("report");
  if (rep && rep.textContent.includes("verifica in corso")) {
    rep.querySelectorAll("em").forEach((em) => {
      if (/^\s*verifica in corso(\.\.\.|…)?\s*$/i.test(em.textContent)) em.remove();
    });
  }
  // a fine ricerca (solo run LIVE avviato qui) il Report va in primo piano:
  // il replay di un run storico non deve sovrascrivere la preferenza utente
  if (state.runId && state.runId === state.activeRunId) {
    setActivityCollapsed(true, true);
  }
  tickTime();
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

/* durata: live se il run mostrato è in corso, altrimenti quella salvata
   calcolata da started_at/ended_at; timestamp mancanti/invalidi -> "—" */
function tickTime() {
  let txt = "—";
  if (state.status === "running" && state.startedAt) {
    txt = fmtDur((Date.now() - state.startedAt) / 1000);
  } else if (state.startedAt && state.endedAt) {
    txt = fmtDur((state.endedAt - state.startedAt) / 1000);
  }
  $("sb-time").textContent = txt;
  const rd = $("rh-duration");
  if (rd) rd.textContent = txt;
}

/* ============================== report ============================== */
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* FIX 10: applica una trasformazione SOLO ai segmenti di testo HTML,
   mai dentro i tag/attributi generati (<a href=...>, data-n=..., ecc.) */
function mapTextSegs(html, fn) {
  return html.split(/(<[^>]*>)/g).map((part) =>
    (part.startsWith("<") && part.endsWith(">")) ? part : fn(part)).join("");
}

/* FIX 10: renderer inline completo. Ordine SICURO:
   1) escape HTML  2) code span -> segnaposto (contenuto resta letterale)
   3) link markdown [t](url) e URL nudi -> <a> con testo protetto da segnaposto
   4) grassetto/corsivo (** * __ _) su soli segmenti di testo
   5) citazioni [n] -> bottoni, per ULTIME e solo su testo (mai in attributi)
   6) ripristino segnaposto */
function renderInline(text) {
  const stash = [];
  const park = (html) => { stash.push(html); return `\u0000${stash.length - 1}\u0000`; };
  let s = escapeHtml(text);
  /* 2) code span */
  s = s.replace(/`([^`\n]+)`/g, (m, c) => park(`<code>${c}</code>`));
  /* 3) link markdown: il testo del link non deve subire corsivi/citazioni */
  s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g,
    (m, t, u) => park(`<a href="${u}" target="_blank" rel="noopener">${t}</a>`));
  /* 3b) URL nudi (solo segmenti di testo: mai dentro href gia' generato) */
  s = mapTextSegs(s, (t) => t.replace(/(^|[\s(])(https?:\/\/[^\s<)"']+)/g,
    (m, pre, url) => pre + park(`<a href="${url}" target="_blank" rel="noopener">${url}</a>`)));
  /* 4) grassetto e corsivo (annidamento gestito dall'ordine *** -> ** -> *);
     il grassetto ammette * singoli nel contenuto (es. **a *b* c**) */
  s = mapTextSegs(s, (t) => t
    .replace(/\*\*\*(\S(?:[\s\S]*?\S)?)\*\*\*/g, "<b><em>$1</em></b>")
    .replace(/\*\*(\S(?:[\s\S]*?\S)?)\*\*/g, "<b>$1</b>")
    .replace(/\*(\S(?:[^*\n]*\S)?)\*/g, "<em>$1</em>")
    .replace(/(?<![\w\\])___(\S(?:[\s\S]*?\S)?)___(?!\w)/g, "<b><em>$1</em></b>")
    .replace(/(?<![\w\\])__(\S(?:[\s\S]*?\S)?)__(?!\w)/g, "<b>$1</b>")
    .replace(/(?<![\w\\])_(\S(?:[^_\n]*\S)?)_(?!\w)/g, "<em>$1</em>"));
  /* 5) citazioni: accorpa adiacenti identici AL SOLO RENDER: [13] [13] -> [13],
     poi bottone; solo su segmenti di testo (href/data-n mai toccati) */
  s = mapTextSegs(s, (t) => t
    .replace(/\[(\d+)\](?:\s*\[\1\])+/g, "[$1]")
    .replace(/\[(\d+)\]/g, '<button class="citation" data-n="$1">[$1]</button>'));
  /* 6) ripristino segnaposto (unico passaggio: gli stash non contengono
     segnaposto) */
  s = s.replace(/\u0000(\d+)\u0000/g, (m, i) => stash[Number(i)] ?? m);
  return s;
}

/* FIX 10: elenco markdown semplice/annidato -> <ul>/<ol> per livello,
   profondita' normalizzata sul rango dell'indentazione (0/4 spazi = 2 livelli) */
function renderList(items) {
  const indents = [...new Set(items.map((it) => it.indent))].sort((a, b) => a - b);
  const root = { children: [], ordered: items[0].ordered };
  const stack = [{ depth: -1, node: root }];
  for (const it of items) {
    const node = { text: it.text, ordered: it.ordered, children: [] };
    const depth = indents.indexOf(it.indent);
    while (stack.length > 1 && stack[stack.length - 1].depth >= depth) stack.pop();
    stack[stack.length - 1].node.children.push(node);
    stack.push({ depth, node });
  }
  const build = (list) => {
    const tag = list.ordered ? "ol" : "ul";
    let html = `<${tag}>`;
    for (const n of list.children) {
      html += `<li>${renderInline(n.text)}`;
      if (n.children.length) html += build(n);
      html += "</li>";
    }
    return html + `</${tag}>`;
  };
  return build(root);
}

/* tabella markdown -> wrapper scrollabile orizzontale (non allarga la pagina) */
function renderMdTable(rows) {
  const cells = (r) => r.replace(/^\|/, "").replace(/\|$/, "").split("|")
    .map((c) => c.trim());
  let html = '<div class="table-wrap"><table>';
  let bodyRow = 0;
  for (const raw of rows) {
    const cs = cells(raw);
    if (cs.every((c) => /^:?-{2,}:?$/.test(c) || c === "")) continue;  // separatore
    const tag = bodyRow === 0 ? "th" : "td";
    html += "<tr>" + cs.map((c) => `<${tag}>${renderInline(c)}</${tag}>`).join("") + "</tr>";
    bodyRow += 1;
  }
  return html + "</table></div>";
}

/* FIX 10: rendering blocco per blocco con ordine sicuro; FIX 9: gestione
   scroll — primo render di un run = sempre in cima ("successo [6]." era la
   posizione scrollata male), aggiornamenti in streaming NON spostano lo
   scroll se l'utente sta leggendo, ripristino solo per run gia' consultato */
function renderReport(markdown) {
  state.reportMarkdown = markdown;
  const sc = $("report-scroll");
  const prevTop = sc ? sc.scrollTop : 0;
  const isUpdate = !!state.lastReportRun && state.lastReportRun === state.runId;
  const out = [];
  const lines = String(markdown).split("\n");
  let i = 0;
  while (i < lines.length) {
    const t = lines[i].trim();
    if (!t) { out.push('<div class="sp"></div>'); i += 1; continue; }
    if (t.startsWith("|") && t.endsWith("|")) {
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        rows.push(lines[i].trim()); i += 1;
      }
      out.push(renderMdTable(rows));
      continue;
    }
    if (/^>{1,}/.test(t)) {  // blockquote (anche senza spazio dopo >)
      const buf = [];
      while (i < lines.length && /^>{1,}/.test(lines[i].trim())) {
        buf.push(lines[i].trim().replace(/^>{1,}[ \t]?/, "")); i += 1;
      }
      out.push(`<blockquote>${renderInline(buf.join(" "))}</blockquote>`);
      continue;
    }
    const li = t.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (li) {  // gruppo di voci consecutive (anche annidate)
      const items = [];
      while (i < lines.length) {
        const m = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
        if (!m) break;
        items.push({ indent: m[1].replace(/\t/g, "  ").length,
                     ordered: /\d/.test(m[2]), text: m[3] });
        i += 1;
      }
      out.push(renderList(items));
      continue;
    }
    const h = t.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      const lvl = Math.min(h[1].length + 1, 6);
      out.push(`<h${lvl}>${renderInline(h[2])}</h${lvl}>`);
      i += 1;
      continue;
    }
    if (/^([-*_])\1{2,}$/.test(t)) { out.push("<hr>"); i += 1; continue; }
    out.push(`<p>${renderInline(t)}</p>`);
    i += 1;
  }
  $("report").innerHTML = out.join("");
  buildToc();
  if (sc) {
    if (isUpdate) {
      // bozza in streaming / finale: lo scroll resta dove legge l'utente,
      // in cima resta in cima
      sc.scrollTop = prevTop > 2 ? Math.min(prevTop, sc.scrollHeight) : 0;
    } else {
      // primo render del run: in cima, a meno che il run sia gia' stato
      // consultato in questa sessione (posizione utente salvata)
      const saved = state.reportScroll.get(state.runId) || 0;
      sc.scrollTop = saved > 2 ? saved : 0;
      state.lastReportRun = state.runId;
    }
  }
}

/* indice delle sezioni: lista cliccabile dagli h2/h3/h4, solo se >= 3 titoli.
   FIX 14: <details> richiudibile con altezza non fissa */
function buildToc() {
  const rep = $("report"), toc = $("report-toc");
  const list = $("toc-list"), sum = $("toc-summary");
  if (!rep || !toc || !list) return;
  const heads = Array.from(rep.querySelectorAll("h2, h3, h4"));
  if (heads.length < 3) { toc.hidden = true; list.innerHTML = ""; return; }
  heads.forEach((h, i) => { if (!h.id) h.id = `sec-${i + 1}`; });
  list.innerHTML = heads.map((h) => {
    const lvl = h.tagName.toLowerCase();
    return `<li class="toc-${lvl}"><a href="#${h.id}">${escapeHtml(h.textContent)}</a></li>`;
  }).join("");
  if (sum) sum.textContent = `indice · ${heads.length} sezioni`;
  toc.hidden = false;
  const smooth = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
  list.querySelectorAll("a").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    const t = document.getElementById(a.getAttribute("href").slice(1));
    if (t) t.scrollIntoView({ behavior: smooth, block: "start" });
  }));
}

/* ── ricerca nel testo del report: evidenzia con <mark> e conta ─────────── */
let _searchTimer = null;
function onReportSearch(v) {
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(() => highlightReport(v), 200);
}
function highlightReport(q) {
  if (state.reportMarkdown != null) renderReport(state.reportMarkdown);  // rebuild pulito
  const nEl = $("report-search-n");
  q = (q || "").trim();
  if (!q) { if (nEl) nEl.textContent = ""; return; }
  const root = $("report");
  if (!root) return;
  const rx = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement &&
      n.parentElement.closest("button.citation"))
      ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  });
  const nodes = [];
  let n;
  while ((n = walker.nextNode())) nodes.push(n);
  let count = 0;
  for (const node of nodes) {
    const text = node.nodeValue;
    rx.lastIndex = 0;
    if (!rx.test(text)) continue;
    rx.lastIndex = 0;
    const frag = document.createDocumentFragment();
    let last = 0, m;
    while ((m = rx.exec(text))) {
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      const mk = document.createElement("mark");
      mk.textContent = m[0];
      frag.appendChild(mk);
      last = m.index + m[0].length;
      count += 1;
      if (m[0].length === 0) rx.lastIndex += 1;
    }
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    node.parentNode.replaceChild(frag, node);
  }
  if (nEl) nEl.textContent = `${count} trovati`;
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
    (c) => Number(c.final_citation_no ?? c.citation_no) === Number(n));
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
    // run storico: mostra coerenza + stats verifica dai dati del DB
    const cb = $("coherence-btn");
    if (cb && state.reportJson.report_markdown) cb.hidden = false;
    if (state.reportJson.verifications) {
      // FIX 11: fonte unica live/storico — stessa striscia con dettaglio
      // [support:X]; i dati restano in cache per i filtri della vista Verifiche
      state.verifsData = state.reportJson;
      renderVerifyStrip(summarizeVerifs(state.reportJson));
    }
  } catch (e) {
    if (String(e.message).startsWith("404")) {
      return { claims: [], evidences: [], sources: [], verifications: [],
               notFound: true };
    }
    throw e;
  }
  return state.reportJson;
}

/* ── selezione citazione: evidenzia il bottone e ricorda il focus ───────── */
function selectCitation(btn) {
  document.querySelectorAll("button.citation.citation-sel")
    .forEach((b) => b.classList.remove("citation-sel"));
  btn.classList.add("citation-sel");
  state.lastCitationBtn = btn;
}

function closePopover(refocus = false) {
  $("popover").hidden = true;
  const hint = $("popover-hint");
  if (hint) hint.hidden = false;
  document.querySelectorAll("button.citation.citation-sel")
    .forEach((b) => b.classList.remove("citation-sel"));
  // Escape: il focus torna all'ultimo bottone citazione cliccato
  if (refocus && state.lastCitationBtn && document.contains(state.lastCitationBtn)) {
    state.lastCitationBtn.focus();
  }
}

/* ============================== history ============================== */
/* B1-fix3: la lista non viene più svuotata e ricostruita da zero a ogni
   refresh (perdeva la posizione di scroll e faceva "saltare" la colonna):
   si aggiornano solo le righe cambiate, l'ordine resta stabile. */
async function refreshHistory() {
  let runs;
  try { runs = await apiJson("/api/runs"); } catch { return; }  // server giù: niente bounce
  const ul = $("history-list");
  const hc = $("history-count");
  const n = Math.min(runs.length, 30);
  if (hc) hc.textContent = String(runs.length);
  const existing = new Map(Array.from(ul.children)
    .map((li) => [li.dataset.runId, li]));
  const seen = new Set();
  for (const r of runs.slice(0, 30)) {
    seen.add(r.id);
    const date = r.started_at
      ? new Date(r.started_at * 1000).toLocaleString("it-IT",
        { dateStyle: "short", timeStyle: "short" }) : "";
    // stati della cronologia in italiano, uniformi alla statusbar
    const stLabel = STATUS_IT[r.status] || r.status;
    const html =
      `<div class="q">${escapeHtml(String(r.question).slice(0, 90))}</div>` +
      `<div class="meta"><span class="badge ${escapeHtml(r.status)}">` +
      `${escapeHtml(stLabel)}</span><span>${escapeHtml(date)}</span>` +
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
    let li = existing.get(r.id);
    if (li) {
      // aggiorna solo se qualcosa è cambiato (niente flicker/scroll-jump)
      if (li.dataset.sig !== `${r.status}|${date}`) {
        li.innerHTML = html;
        li.dataset.sig = `${r.status}|${date}`;
        li.dataset.q = (r.question || "").toLowerCase();
        wireHistoryItem(li, r);
      }
    } else {
      li = document.createElement("li");
      li.dataset.runId = r.id;
      li.dataset.q = (r.question || "").toLowerCase();
      li.dataset.sig = `${r.status}|${date}`;
      li.innerHTML = html;
      wireHistoryItem(li, r);
      // inserimento in posizione (la lista è ordinata come l'API: più recente prima)
      const anchor = ul.children[Math.min(runs.slice(0, 30).indexOf(r), ul.children.length)];
      if (anchor && anchor !== li) ul.insertBefore(li, anchor);
      else if (!anchor) ul.appendChild(li);
    }
    li.classList.toggle("active", r.id === state.runId);
  }
  // rimuovi le righe di run non più esistenti (eliminati)
  for (const [id, li] of existing) {
    if (!seen.has(id)) li.remove();
  }
  // FIX 13: "completata con N avvisi" accanto allo stato, STESSA fonte del
  // run-header (report.json); il badge e' cliccabile come quello in header
  const completed = runs.slice(0, 30).filter((r) => r.status === "completed");
  runLimited(completed.map((r) => async () => {
    const w = await histWarnings(r.id);
    if (!w || !w.total) return;
    const li = ul.querySelector(`li[data-run-id="${CSS.escape(r.id)}"]`);
    const meta = li && li.querySelector(".meta");
    if (!meta || li.querySelector(".wbadge")) return;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "badge completed-warnings wbadge";
    b.textContent = `completata con ${w.total} avvisi`;
    b.title = "Mostra il dettaglio degli avvisi di questa ricerca";
    const st = meta.querySelector(".badge");
    if (st) st.replaceWith(b); else meta.prepend(b);
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      loadRun(r.id, { openWarnings: true });
    });
  }), 4);
}

function wireHistoryItem(li, r) {
  li.onclick = (e) => {
    if (e.target.closest("[data-del]")) return;
    loadRun(r.id);
  };
  const del = li.querySelector("[data-del]");
  if (del) del.onclick = () => deleteRun(r.id);
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
    const st = isActiveRun ? "running" : (run.status || "completed");
    state.dbStatus = isActiveRun ? null : (run.status || null);
    setStatus(st);
    // durata: NESSUN timer per i run chiusi; quella salvata da started/ended_at.
    // Il timer riparte solo se il run è davvero ancora in corso.
    state.startedAt = run.started_at ? run.started_at * 1000 : null;
    state.endedAt = run.ended_at ? run.ended_at * 1000 : null;
    stopTimer();
    if (st === "running" && state.startedAt) startTimer();
    tickTime();
    state.loadedRun = {
      question: run.question || "",
      depth: run.depth || "",
      language: run.language || "auto",
    };
    updateRunHeader(run);
    // ragione di arresto dai dati del DB (limits_json contiene stop_reason)
    let stopCode = null;
    try { stopCode = (JSON.parse(run.limits_json || "null") || {}).stop_reason || null; }
    catch { /* limits_json malformato: nessuna ragione mostrata */ }
    setStopReason(st === "running" ? null : stopCode, st);
    listenEvents(runId); // replay + chiusura
    updateStatusbar();
    if (detail.report) initReportViews();
    if (opts.openWarnings) {
      // FIX 13: click sul badge avvisi della cronologia = apri il run E il
      // dettaglio degli avvisi (come il badge cliccabile del run-header)
      await refreshWarnings();
      if (state.warnings && state.warnings.total > 0) renderWarningsPanel();
      else toast("Nessun avviso registrato per questa ricerca.");
    } else if (st === "completed" || st === "completed-warnings") {
      refreshWarnings();
    }
  } catch (e) {
    toast(`Errore caricamento: ${e.message}`);
    setStatus(state.activeRunId ? "running" : "idle");
  }
}

async function deleteRun(runId) {
  if (!confirm("Eliminare definitivamente questo run?")) return;
  try {
    await api(`/api/runs/${runId}`, { method: "DELETE" });
    if (state.runId === runId) {
      resetPanels();
      setStatus("idle");
      state.runId = null;
    }
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
  stopTimer();
  $("activity").innerHTML = "";
  $("feed-count").textContent = "0 eventi";
  setReportEmpty();
  $("plan").hidden = true;
  closePopover();
  setAction("", false);
  state.reading = 0;
  state.failedChecks = 0;
  state.warnings = null;
  state.warningsTotal = 0;
  state.seenPageEvents = new Set();
  const ri = $("reading-indicator");
  if (ri) ri.hidden = true;
  const ct = $("cycle-tabs");
  if (ct) ct.innerHTML = "";
  resetStepper();
  const cb = $("coherence-btn");
  if (cb) cb.hidden = true;
  const cp = $("coherence-panel");
  if (cp) { cp.hidden = true; cp.innerHTML = ""; }
  const wp = $("warnings-panel");
  if (wp) { wp.hidden = true; wp.innerHTML = ""; }
  const vs = $("verify-stats");
  if (vs) { vs.hidden = true; vs.innerHTML = ""; }
  state.cycle = null;
  state.counters = { sources: 0, evidence: 0, queries: 0, claims: 0,
                     tokens: 0, llm: 0 };
  state.reportJson = null;
  state.reportMarkdown = null;
  state.startedAt = null;
  state.endedAt = null;
  state.dbStatus = null;
  state.loadedRun = null;
  state.srcRows = null;
  // FIX 11/9: filtro verifiche e posizione di scroll ripartono puliti;
  // reportScroll (posizioni per run consultati) resta in memoria di sessione
  state.verifsFilter = null;
  state.verifsData = null;
  state.cycleStats = null;
  state.lastReportRun = null;
  const rc = $("rh-claims");
  if (rc) { rc.hidden = true; rc.textContent = ""; }
  setStopReason(null);
  // viste report: torna al Report, tab nascoste finché non ci sono dati reali
  const rs = $("report-search");
  if (rs) rs.value = "";
  const rn = $("report-search-n");
  if (rn) rn.textContent = "";
  setReportView("report");
  const ts = $("report-tabs");
  if (ts) {
    ts.querySelector('[data-view="sources"]').hidden = true;
    ts.querySelector('[data-view="verifs"]').hidden = true;
  }
  const tsn = $("tab-sources-n");
  if (tsn) tsn.textContent = "";
  const tvn = $("tab-verifs-n");
  if (tvn) tvn.textContent = "";
  const vr = $("view-sources");
  if (vr) vr.innerHTML = "";
  const vv = $("view-verifs");
  if (vv) vv.innerHTML = "";
  const rh = $("run-header");
  if (rh) rh.hidden = true;
  applyActivityPref();
  $("sb-time").textContent = "—";
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
    state.startedAt = Date.now();
    state.endedAt = null;
    state.loadedRun = { question, depth: body.depth, language: $("language").value };
    updateRunHeader({ question, started_at: Date.now() / 1000 });
    setStatus("running");
    startTimer();
    tickTime();
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

/* ====================== run header + arresto ====================== */
function updateRunHeader(run) {
  const rh = $("run-header");
  if (!rh || !run) return;
  rh.hidden = false;
  $("rh-question").textContent = run.question || "";
  $("rh-date").textContent = run.started_at
    ? new Date(run.started_at * 1000).toLocaleString("it-IT",
      { dateStyle: "short", timeStyle: "short" }) : "—";
  const rb = $("repeat-btn");
  if (rb) rb.hidden = !state.loadedRun;
  tickTime();
}

/* etichetta ragione di arresto vicino allo stato (solo a fine ricerca) */
function setStopReason(code, finalStatus) {
  const el = $("rh-stop");
  if (!el) return;
  let label = null;
  if (code && STOP_IT[code]) label = STOP_IT[code];
  else if (code) label = String(code).toLowerCase().replace(/_/g, " ");
  else if (finalStatus === "failed") label = "errore";
  state.stopReason = label;
  if (!label) { el.hidden = true; el.textContent = ""; return; }
  el.textContent = `arresto: ${label}`;
  el.hidden = false;
}

/* ====================== collasso pannello Attività ====================== */
/* FIX 8: collassato = la sola riga header "Mostra attivita — N eventi"
   (chevron ruotato + titolo che lo dice); il conteggio resta quello reale
   degli eventi visualizzabili e il click riapre il corpo completo */
function setActivityCollapsed(collapsed, save) {
  const panel = $("activity-panel");
  if (!panel) return;
  panel.classList.toggle("collapsed", collapsed);
  const head = $("activity-head");
  if (head) head.setAttribute("aria-expanded", collapsed ? "false" : "true");
  const ttl = $("activity-ttl");
  if (ttl) ttl.textContent = collapsed ? "Mostra attivita" : "Attivita";
  if (save) localStorage.setItem("dr_activity", collapsed ? "1" : "0");
}
function applyActivityPref() {
  setActivityCollapsed(localStorage.getItem("dr_activity") === "1", false);
}

/* ====================== modalità lettura ====================== */
function setReading(on, save = true) {
  document.body.classList.toggle("reading", on);
  const b = $("reading-mode-btn");
  if (b) {
    b.setAttribute("aria-pressed", on ? "true" : "false");
    b.classList.toggle("on", on);
    b.title = on ? "Esci dalla modalità lettura (Esc)"
                 : "Modalità lettura: nasconde le colonne laterali";
  }
  if (save) localStorage.setItem("dr_reading", on ? "1" : "0");
}

/* ====================== avvisi consultabili ====================== */
/* problemi del risultato: verifiche FAILED/WEAKENED (frase + motivo);
   problemi operativi: fonti non lette (url + errore). Dati da report.json.
   FIX 13: la stessa funzione alimenta il badge della cronologia, cosi'
   cronologia e run-header mostrano SEMPRE lo stesso "con N avvisi". */
function summarizeRunWarnings(data) {
  if (!data || data.notFound) return { result: [], ops: [], total: 0 };
  const claimsById = new Map((data.claims || []).map((c) => [c.id, c]));
  const result = [], ops = [];
  for (const v of data.verifications || []) {
    if (v.verdict === "FAILED" || v.verdict === "WEAKENED") {
      const c = claimsById.get(v.claim_id);
      result.push({
        kind: v.verdict,
        text: (c && c.text) || "(claim non disponibile)",
        reason: stripSupportPrefix(v.reason),
      });
    }
  }
  for (const s of data.sources || []) {
    if (s.error || s.fetch_status === "FETCH_FAILED" ||
        s.fetch_status === "EXTRACTION_FAILED") {
      ops.push({ url: s.url_final || s.url || "",
                 error: s.error || FETCH_IT[s.fetch_status] || "errore" });
    }
  }
  return { result, ops, total: result.length + ops.length };
}

async function computeWarnings() {
  const data = await loadReportJson();
  if (!data || data.notFound) return null;
  return summarizeRunWarnings(data);
}

/* FIX 13: avvisi per run storico (cache per id; fetch report.json a bassa
   concorrenza solo per i run completati visibili in lista) */
const _histWarn = new Map();
function histWarnings(runId) {
  if (!_histWarn.has(runId)) {
    _histWarn.set(runId, apiJson(`/api/runs/${runId}/report.json`)
      .then((d) => summarizeRunWarnings(d))
      .catch(() => null));
  }
  return _histWarn.get(runId);
}

/* esegue task asincroni con concorrenza limitata (coda di fetch) */
async function runLimited(tasks, limit) {
  const queue = tasks.slice();
  const n = Math.max(1, Math.min(limit, queue.length));
  await Promise.all(Array.from({ length: n }, async () => {
    while (queue.length) {
      const t = queue.shift();
      try { await t(); } catch { /* i singoli fallimenti non bloccano gli altri */ }
    }
  }));
}

async function refreshWarnings() {
  try {
    const w = await computeWarnings();
    state.warnings = w;
    state.warningsTotal = w ? w.total : 0;
    if (w && w.total > 0 &&
        (state.status === "completed" || state.status === "completed-warnings")) {
      setStatus("completed-warnings");
    } else if ((!w || w.total === 0) && state.status === "completed-warnings") {
      setStatus("completed");
    }
    const wp = $("warnings-panel");
    if (wp && !wp.hidden) renderWarningsPanel();
  } catch { /* report non disponibile: nessun avviso calcolabile */ }
}

function toggleWarningsPanel() {
  const wp = $("warnings-panel");
  if (!wp || !(state.warningsTotal > 0)) return;
  if (wp.hidden) renderWarningsPanel();
  else wp.hidden = true;
}

function renderWarningsPanel() {
  const wp = $("warnings-panel");
  const w = state.warnings;
  if (!wp || !w || !w.total) { if (wp) wp.hidden = true; return; }
  const parts = [`<div class="coherence-head"><span class="c-badge">${w.total} avvisi</span>` +
    `<span class="cr">dettaglio degli avvisi della ricerca</span>` +
    `<button id="warnings-close" class="hbtn" type="button" aria-label="Chiudi avvisi" title="Chiudi">` +
    `<svg class="ic s12" aria-hidden="true"><use href="#i-x"/></svg></button></div>`];
  if (w.result.length) {
    parts.push('<div class="w-sec">Problemi del risultato</div><ul>');
    for (const r of w.result) {
      parts.push(`<li><b class="ct ${r.kind === "FAILED" ? "t-unsupported" : "t-uncovered"}">` +
        `${r.kind === "FAILED" ? "verifica fallita" : "verifica indebolita"}</b>` +
        `${escapeHtml(r.text.slice(0, 180))}` +
        (r.reason ? `<span class="cr"> — ${escapeHtml(r.reason.slice(0, 160))}</span>` : "") +
        `</li>`);
    }
    parts.push("</ul>");
  }
  if (w.ops.length) {
    parts.push('<div class="w-sec">Problemi operativi</div><ul>');
    for (const o of w.ops) {
      parts.push(`<li><b class="ct t-unknown_url">fonte non letta</b>` +
        `<span class="mono">${escapeHtml(shortUrl(o.url))}</span>` +
        `<span class="cr"> — ${escapeHtml(String(o.error).slice(0, 140))}</span></li>`);
    }
    parts.push("</ul>");
  }
  wp.innerHTML = parts.join("");
  wp.hidden = false;
  const xc = $("warnings-close");
  if (xc) xc.addEventListener("click", () => { wp.hidden = true; });
}

/* ====================== viste Report / Fonti / Verifiche ====================== */
function setReportView(name) {
  state.reportView = name;
  const map = { report: "view-report", sources: "view-sources", verifs: "view-verifs" };
  for (const [view, id] of Object.entries(map)) {
    const el = $(id);
    if (el) el.hidden = view !== name;
  }
  document.querySelectorAll("#report-tabs .rtab").forEach((b) => {
    const on = b.dataset.view === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
}

/* carica report.json e popola tab + viste (solo con dati reali) */
async function initReportViews() {
  try {
    const data = await loadReportJson();
    if (!data || data.notFound) return;
    const nSrc = (data.sources || []).length;
    const nVer = (data.verifications || []).length;
    const tabSrc = document.querySelector('#report-tabs [data-view="sources"]');
    const tabVer = document.querySelector('#report-tabs [data-view="verifs"]');
    if (tabSrc) tabSrc.hidden = nSrc === 0;
    if (tabVer) tabVer.hidden = nVer === 0;
    if (nSrc) {
      renderSourcesView(data);
      const t = $("tab-sources-n");
      if (t) t.textContent = String(nSrc);
    }
    if (nVer) {
      renderVerifsView(data);
      const t = $("tab-verifs-n");
      if (t) t.textContent = String(nVer);
    }
    if ((nSrc === 0 && state.reportView === "sources") ||
        (nVer === 0 && state.reportView === "verifs")) {
      setReportView("report");
    }
  } catch { /* report.json non disponibile: restano solo il Report */ }
}

/* numero di citazioni del report che poggiano su ciascuna fonte */
function citationCounts(data) {
  const bySource = new Map();
  const evById = new Map((data.evidences || []).map((e) => [e.evidence_id, e]));
  for (const c of data.claims || []) {
    const n = c.final_citation_no ?? c.citation_no;
    if (n == null) continue;
    for (const eid of c.evidence_ids || []) {
      const e = evById.get(eid);
      if (!e || !e.source_id) continue;
      if (!bySource.has(e.source_id)) bySource.set(e.source_id, new Set());
      bySource.get(e.source_id).add(n);
    }
  }
  return bySource;
}

/* ── riepilogo per ciclo (dal feed eventi già caricato) ────────────────────
   Accumula gli eventi per ciclo durante replay/live: cycle_contribution (e
   cycle_registry se presente) per le evidenze aggiunte, più le fonti viste
   nel ciclo (nuove/riusi/errori/duplicati). Se il feed non contiene eventi
   cycle_* il blocco non viene mostrato. I conteggi fonti sono URL distinti. */
function cycleBucket(cycle) {
  if (!state.cycleStats) state.cycleStats = new Map();
  let b = state.cycleStats.get(cycle);
  if (!b) {
    b = { registered: false, newUrls: new Set(), reuseUrls: new Set(),
          errUrls: new Set(), dupUrls: new Set(), evidences: null };
    state.cycleStats.set(cycle, b);
  }
  return b;
}

function trackCycleEvent(type, cycle, p) {
  if (!cycle) return;
  const b = cycleBucket(cycle);
  if (type === "cycle_contribution" || type === "cycle_registry") {
    b.registered = true;
    if (p && p.new_evidences != null) b.evidences = p.new_evidences;
  } else if (type === "source_fetched") {
    const u = p.url || "";
    if (u) (p.reused ? b.reuseUrls : b.newUrls).add(u);
  } else if (type === "source_failed") {
    if (p.url) b.errUrls.add(p.url);
  } else if (type === "source_duplicate") {
    if (p.url) b.dupUrls.add(p.url);
  } else {
    return;
  }
  updateCycleBlock();
}

/* righe del blocco "Per ciclo"; "" se nessun evento cycle_* nel feed */
function cycleBreakdownInner() {
  if (!state.cycleStats) return "";
  const cycles = [...state.cycleStats.entries()]
    .filter(([, b]) => b.registered)
    .sort((a, b) => a[0] - b[0]);
  if (!cycles.length) return "";
  const rows = cycles.map(([c, b]) => {
    const bits = [
      `<span title="Pagine lette per la prima volta in questo ciclo">` +
        `<b>${b.newUrls.size}</b> nuove</span>`,
      `<span title="Pagine già note riusate (cache)">${b.reuseUrls.size} riusi</span>`,
      `<span title="Lettura fallite in questo ciclo">${b.errUrls.size} errori</span>`,
    ];
    if (b.dupUrls.size) {
      bits.push(`<span title="Duplicati scartati (stesso contenuto di una fonte già acquisita)">${b.dupUrls.size} duplicati</span>`);
    }
    bits.push(`<span title="Evidenze aggiunte nel ciclo (evento cycle_contribution)">` +
      (b.evidences != null ? `<b>+${b.evidences}</b> evidenze` : `evidenze n/d`) +
      `</span>`);
    return `<div class="pc-row mono"><span class="pc-c">ciclo ${c}</span>` +
      bits.join("") + `</div>`;
  }).join("");
  return `<div class="pc-title">Per ciclo` +
    `<span class="faint"> dal feed eventi: fonti nuove/riusi/errori ed evidenze aggiunte</span></div>` + rows;
}

function updateCycleBlock() {
  const el = $("src-per-cycle");
  if (!el) return;   // vista Fonti non ancora renderizzata: solo accumulo
  const inner = cycleBreakdownInner();
  el.innerHTML = inner;
  el.hidden = !inner;
}

function renderSourcesView(data) {
  const box = $("view-sources");
  if (!box) return;
  const bySource = citationCounts(data);
  // duplicati/riusi: stesso content_hash nel run (dedup per contenuto)
  const hashCount = {};
  for (const s of data.sources || []) {
    if (s.content_hash) hashCount[s.content_hash] = (hashCount[s.content_hash] || 0) + 1;
  }
  state.srcRows = (data.sources || []).map((s) => {
    const cites = bySource.get(s.id);
    return {
      s,
      cites: cites ? cites.size : 0,
      nums: cites ? Array.from(cites).sort((a, b) => a - b) : [],
      dup: !!(s.content_hash && hashCount[s.content_hash] > 1),
    };
  });
  const isRead = (r) => r.s.fetch_status === "SUCCESS" ||
    r.s.fetch_status === "PARTIAL_CONTENT";
  const isFailed = (r) => r.s.fetch_status === "FETCH_FAILED" ||
    r.s.fetch_status === "EXTRACTION_FAILED" || !!r.s.error;
  const tot = state.srcRows.length;
  const lette = state.srcRows.filter(isRead).length;
  const fallite = state.srcRows.filter(isFailed).length;
  const duplicati = state.srcRows.filter((r) => r.dup).length;
  const citate = state.srcRows.filter((r) => r.cites > 0).length;
  box.innerHTML =
    `<div class="rview-bar">` +
    `<span class="src-counters mono">` +
    `<span title="Tutte le fonti trovate nel run: ogni riga, incluse fallite e duplicati">trovate <b>${tot}</b></span>` +
    `<span title="Scaricate e lette con successo — stesso criterio del contatore 'fonti' della statusbar">lette <b>${lette}</b></span>` +
    `<span title="Non lette: errore di fetch o di estrazione">fallite <b>${fallite}</b></span>` +
    `<span title="Stesso contenuto di un'altra fonte già acquisita nel run">duplicati <b>${duplicati}</b></span>` +
    `<span title="Citata almeno una volta nel report finale">citata <b>${citate}</b></span></span>` +
    `<input id="src-filter" type="search" placeholder="filtra fonti…" autocomplete="off" ` +
    `aria-label="Filtra le fonti"></div>` +
    `<div id="src-per-cycle" class="src-per-cycle" hidden></div>` +
    `<ul id="src-list" class="src-list"></ul>`;
  renderSrcList("");
  updateCycleBlock();
  const f = $("src-filter");
  if (f) f.addEventListener("input", (e) => renderSrcList(e.target.value));
}

function renderSrcList(q) {
  const ul = $("src-list");
  if (!ul || !state.srcRows) return;
  q = (q || "").toLowerCase().trim();
  const rows = state.srcRows.filter(({ s }) => !q ||
    `${s.title || ""} ${s.url || ""} ${s.domain || ""}`.toLowerCase().includes(q));
  ul.innerHTML = rows.map(({ s, cites, nums, dup }) => {
    const url = s.url_final || s.url || "";
    // badge di stato per riga: letta (ok) / fallita (errore) / duplicata
    let stBadge = "";
    if (s.fetch_status) {
      const okSt = s.fetch_status === "SUCCESS" ||
        s.fetch_status === "PARTIAL_CONTENT";
      stBadge = `<span class="badge ${okSt ? "st-ok" : "st-bad"}" ` +
        `title="Stato di acquisizione: ${escapeHtml(s.fetch_status)}">` +
        `${escapeHtml(FETCH_IT[s.fetch_status] || s.fetch_status)}</span>`;
    }
    const bits = [];
    if (s.source_type) bits.push(`tipo ${escapeHtml(s.source_type)}`);
    if (!s.fetch_status && s.http_status) bits.push(`http ${s.http_status}`);
    if (s.published_at) bits.push(escapeHtml(String(s.published_at).slice(0, 10)));
    return `<li>` +
      `<div class="src-line"><a href="${escapeHtml(url)}" target="_blank" rel="noopener">` +
      `${escapeHtml(s.title || url)}</a>` +
      stBadge +
      (dup ? '<span class="badge neutral" title="Contenuto duplicato di un\'altra fonte nel run">duplicata</span>' : "") +
      `</div>` +
      `<div class="src-meta">` +
      (s.domain ? `<span>${escapeHtml(s.domain)}</span>` : "") +
      bits.map((b) => `<span>${b}</span>`).join("") +
      (cites ? `<span class="src-cites" title="Citazioni di questa fonte nel report">` +
        `[${nums.join("][")}] · ${cites} ${cites === 1 ? "citazione" : "citazioni"}</span>`
             : '<span class="faint">non citata</span>') +
      `</div></li>`;
  }).join("") || '<li class="dim">Nessuna fonte corrisponde al filtro.</li>';
}

/* prefisso [support:X] scritto dal verifier in verdict_reason */
function parseSupport(reason) {
  const m = /^\[support:(supported|contradicted|insufficient|mixed)\]\s*/
    .exec(String(reason || ""));
  return m ? { support: m[1], rest: reason.slice(m[0].length) }
           : { support: null, rest: String(reason || "") };
}

function renderVerifsView(data) {
  const box = $("view-verifs");
  if (!box) return;
  state.verifsData = data;
  const claimsById = new Map((data.claims || []).map((c) => [c.id, c]));
  const evById = new Map((data.evidences || []).map((e) => [e.evidence_id, e]));
  // FIX 12 (FAILED): la frase e' ancora nel report finale o gia' gestita?
  const md = normText(String(data.report_markdown || ""));
  const inReport = (c) => {
    const t = normText(c && c.text);
    return t.length >= 12 && md.includes(t);
  };
  // conteggi per i chip di filtro
  const counts = {};
  for (const d of VERIFS_FILTER_DEFS) {
    counts[d.key] = (data.verifications || [])
      .filter((v) => verifsMatch(v, parseFilterKey(d.key))).length;
  }
  const f = state.verifsFilter || { kind: "all" };
  const curKey = f.kind === "all" ? "all" : `${f.kind}:${f.value}`;
  const rows = (data.verifications || []).filter((v) => verifsMatch(v, f));
  const parts = ['<div class="vf-bar">' + VERIFS_FILTER_DEFS.map((d) => {
    const on = d.key === curKey ? " on" : "";
    return `<button type="button" class="vf-chip${on}" data-filter="${d.key}" ` +
      `title="Filtra le verifiche per stato: ${d.label}">${d.label} <b>${counts[d.key] || 0}</b></button>`;
  }).join("") + "</div>",
    '<div class="verif-legend mono">stati di supporto: ' +
    Object.keys(SUPPORT_IT).map((k) => `<b>${k}</b> = ${SUPPORT_HELP[k]}`).join(" · ") +
    '</div><ul class="verif-list">'];
  if (!rows.length) {
    parts.push('<li class="dim">Nessuna verifica per questo filtro.</li>');
  }
  for (const v of rows) {
    const c = claimsById.get(v.claim_id);
    const { support, rest } = parseSupport(v.reason);
    const cause = parseCause(v.reason);
    const restTxt = stripCauseToken(rest);
    const n = c ? (c.final_citation_no ?? c.citation_no) : null;
    const evs = c ? (c.evidence_ids || []).map((id) => evById.get(id)).filter(Boolean) : [];
    let evHtml = "";
    if (evs.length) {
      evHtml = n != null
        ? `<button type="button" class="hbtn wide ev-link" data-cit="${n}" ` +
          `title="Apri il dettaglio delle evidenze nel pannello citazioni">` +
          `evidenze (${evs.length})</button>`
        : `<span class="cr mono" title="Quote dell'evidenza">“${escapeHtml(String(evs[0].quote || "").slice(0, 140))}”</span>`;
    }
    // FIX 12: per le non conclusive, distingui frase ancora presente vs gestita
    let presence = "";
    if (v.verdict === "FAILED") {
      const present = inReport(c);
      presence = `<span class="badge neutral" title="${present
        ? "La frase di questo claim è ancora presente nel report finale"
        : "La frase di questo claim non è più nel report finale (già gestita dal verifier)"}">` +
        `${present ? "frase ancora nel report" : "frase non più nel report"}</span>`;
    }
    parts.push(`<li>` +
      `<div class="verif-head"><span class="verdict ${escapeHtml(v.verdict)}">` +
      `${escapeHtml(v.verdict)}</span>` +
      (support ? `<span class="sup sup-${support}" title="${SUPPORT_IT[support]}: ${SUPPORT_HELP[support]}">${SUPPORT_IT[support]}</span>` : "") +
      (cause ? `<span class="sup sup-cause" title="Causa dichiarata nel motivo [cause:${cause}]">${escapeHtml(CAUSE_IT[cause] || cause)}</span>` : "") +
      presence +
      `</div>` +
      `<div class="verif-text">${escapeHtml((c && c.text) || "(claim rimosso dal report)")}</div>` +
      (restTxt ? `<div class="cr">${escapeHtml(restTxt.slice(0, 220))}</div>` : "") +
      (cause && CAUSE_ACTION[cause]
        ? `<div class="cause-next" title="Prossima azione possibile per questa causa">prossima azione: ${escapeHtml(CAUSE_ACTION[cause])}</div>`
        : "") +
      (evHtml ? `<div class="verif-foot">${evHtml}</div>` : "") +
      `</li>`);
  }
  parts.push("</ul>");
  box.innerHTML = parts.join("");
  box.querySelectorAll(".vf-chip").forEach((b) =>
    b.addEventListener("click", () => setVerifsFilter(b.dataset.filter)));
  box.querySelectorAll(".ev-link").forEach((b) => b.addEventListener("click", () => {
    openCitation(b.dataset.cit).catch((x) => toast(x.message));
  }));
}

/* ====================== ripeti ricerca ====================== */
function repeatRun() {
  const r = state.loadedRun;
  if (!r) return;
  if (state.status === "running") { toast("Interrompi il run corrente prima."); return; }
  $("question").value = r.question || "";
  if (r.depth) {
    const opt = $("depth").querySelector(`option[value="${r.depth}"]`);
    if (opt) $("depth").value = r.depth;
  }
  if (r.language) {
    const opt = $("language").querySelector(`option[value="${r.language}"]`);
    $("language").value = opt ? r.language : "auto";
  }
  $("question").focus();
  toast("Domanda e opzioni precompilate: controlla e premi Avvia.");
}

/* ============================== init ============================== */
function init() {
  initTheme();
  // modalità lettura salvata dall'ultima sessione
  setReading(localStorage.getItem("dr_reading") === "1", false);
  $("run-form").addEventListener("submit", startRun);
  $("stop-btn").addEventListener("click", stopRun);
  $("popover-close").addEventListener("click", () => closePopover(true));
  $("history-clear").addEventListener("click", clearHistory);
  $("coherence-btn").addEventListener("click", runCoherence);
  $("history-filter").addEventListener("input", applyHistoryFilter);
  // badge stato cliccabile: apre/chiude il dettaglio degli avvisi
  $("sb-state").addEventListener("click", toggleWarningsPanel);
  // ripeti ricerca: precompila domanda + opzioni
  $("repeat-btn").addEventListener("click", repeatRun);
  // azioni report
  $("copy-btn").addEventListener("click", async () => {
    const md = state.reportMarkdown;
    if (!md) { toast("Nessun report da copiare."); return; }
    try {
      await navigator.clipboard.writeText(md);
      toast("Report copiato negli appunti (markdown).");
    } catch {
      toast("Copia non riuscita: il browser ha negato l'accesso agli appunti.");
    }
  });
  $("print-btn").addEventListener("click", () => {
    if (!state.reportMarkdown) { toast("Nessun report da stampare."); return; }
    setReportView("report");
    window.print();
  });
  $("reading-mode-btn").addEventListener("click", () =>
    setReading(!document.body.classList.contains("reading")));
  // tab Report / Fonti / Verifiche
  $("report-tabs").addEventListener("click", (e) => {
    const b = e.target.closest(".rtab");
    if (b && !b.hidden) setReportView(b.dataset.view);
  });
  // ricerca nel testo del report (highlight <mark> + conteggio)
  $("report-search").addEventListener("input", (e) => onReportSearch(e.target.value));
  // pannello Attività: header cliccabile (collapse/expand)
  const ahead = $("activity-head");
  ahead.addEventListener("click", () =>
    setActivityCollapsed(!$("activity-panel").classList.contains("collapsed"), true));
  ahead.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); ahead.click(); }
  });
  // FIX 9: la posizione di scroll del report e' per-run (ripristino solo per
  // run gia' consultati in questa sessione)
  $("report-scroll").addEventListener("scroll", () => {
    if (state.runId) state.reportScroll.set(state.runId, $("report-scroll").scrollTop);
  }, { passive: true });
  // FIX 11: contatore "claims estratti" cliccabile -> vista Verifiche
  const claimsBox = $("sb-claims-box");
  claimsBox.addEventListener("click", () => setVerifsFilter("all"));
  claimsBox.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setVerifsFilter("all"); }
  });
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
    if (e.key !== "Escape") return;
    // Escape: prima chiude il popover citazione (focus al bottone [n]),
    // altrimenti esce dalla modalità lettura
    if (!$("popover").hidden) closePopover(true);
    else if (document.body.classList.contains("reading")) setReading(false);
  });
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("button.citation");
    if (btn) {
      selectCitation(btn);
      openCitation(btn.dataset.n).catch((x) => toast(x.message));
      return;
    }
    if (!$("popover").hidden && !e.target.closest("#popover")) closePopover();
  });
  loadSettings();
  refreshHistory();
}

document.addEventListener("DOMContentLoaded", init);
