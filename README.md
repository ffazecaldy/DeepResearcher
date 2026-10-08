# Deep Researcher

Research agent autonomo e **auditabile**: data una domanda, cerca sul web in modo iterativo, estrae evidenze con citazioni letterali verificate, individua lacune, scrive un report con provenance completa e verifica ogni claim prima di mostrartelo.

**Principio architetturale**

```
LLM    = pianificazione, lettura, sintesi, report          (generativo)
Laya   = decisioni System 1 tipizzate (YES/NO, scelta, score)  (locale, 1 forward pass)
Evidence Store = verità osservabile raccolta dalle fonti
Provenance     = question → subquestion → query → result → source
                 → chunk → evidence → claim → citation → verification
Verifier       = ultimo controllo prima dell'output
```

## Avvio rapido

```bash
# 1. ambiente
uv venv .venv --python 3.11
uv pip install -p .venv/Scripts/python.exe httpx "pydantic>=2.7" pydantic-settings \
    trafilatura readability-lxml pypdf fastapi "uvicorn[standard]" sse-starlette fpdf2
# opzionale (decision engine System 1 locale, ~1.6 GB):
uv pip install -p .venv/Scripts/python.exe laya

# 2. configurazione
cp .env.example .env    # poi inserisci le tue chiavi in .env (mai nel codice)

# 3. server web (UI realtime)
.venv/Scripts/python.exe -m uvicorn app.server.app:create_app --factory --host 127.0.0.1 --port 8765
# → apri http://127.0.0.1:8765/

# 3b. oppure da CLI
.venv/Scripts/python.exe -m app.cli "La tua domanda" --depth standard
```

## Configurazione (`.env`)

| Variabile | Valori | Note |
|---|---|---|
| `DR_LLM_PROVIDER` | `openai_compat` \| `anthropic` \| `ollama` | testato: openai_compat (z.ai) |
| `DR_LLM_MODEL` / `DR_LLM_BASE_URL` / `DR_LLM_API_KEY` | — | es. z.ai: `glm-5.3-flash` + `https://api.z.ai/api/coding/paas/v4` |
| `DR_SEARCH_PROVIDER` | `duckduckgo` \| `tavily` \| `brave` \| `searxng` | **duckduckgo = zero API key** |
| `DR_DECISION_ENGINE` | `laya` \| `llm` | laya è opzionale: senza, tutto gira su LLM |
| `DR_DECISION_LAYA_DECISIONS` | nomi separati da virgola | default `is_relevant` (benchmark Fase 2) |
| `DR_MAX_CYCLES`, `DR_MAX_TOTAL_PAGES`, `DR_MAX_RUNTIME_SECONDS`, … | interi | limiti hard del run |

Profondità: `rapida`=1 ciclo, `standard`=3, `approfondita`=8 (richiede `DR_MAX_CYCLES>=8`, nuovo default).

## Pipeline

```
Question → Planner(LLM) → Search → Fetch/Extract (SSRF-guard, robots.txt, rate-limit, cache)
→ DecisionEngine (Laya ⟶ fallback LLM, ogni decisione tracciata con engine+latenza)
→ Evidence (solo quote letterali presenti nel testo, span conservati)
→ GapCheck(LLM) → [nuove query? ↺ max cicli] → Writer(LLM, solo evidenze esistenti)
→ Verifier (controlli deterministici + LLM: claim non supportati corretti/rimossi)
→ Report markdown + JSON provenance + PDF
```

Il report non può contenere informazioni non supportate dall'evidence store; ogni citazione `[n]` è cliccabile nella UI e mostra fonte, estratto, evidenza, claim ed esito verifica.

## Benchmark decision engine (Fase 2, 20 casi etichettati, CPU Windows)

| Decisione | Laya zero-shot | Latenza mediana |
|---|---|---|
| `is_relevant` (YES/NO) | **85%** | 85 ms |
| `source_type` (choice) | 30% | 85 ms |
| `quality` (score) | 30% | 85 ms |

Per questo il default è il **routing per-decisione**: `is_relevant`→Laya, resto→LLM. Modificabile via `DR_DECISION_LAYA_DECISIONS`. Riproducibile con `python tests/benchmarks/bench_decisions.py`.

## Test

```bash
.venv/Scripts/python.exe -m pytest tests/ -m "not live"   # suite completa
.venv/Scripts/python.exe -m pytest tests/                 # include 1 test di rete reale
```

Copertura: planner, schemi JSON, normalizzazione/dedup URL, SSRF guard, estrazione, fetcher (server HTTP locale: redirect, robots, 404, vuoti), searcher, reader (quote inventate → scartate), decision engine (Laya fake + fallback), writer, verifier, provenance, server API, E2E mock completo.

## Sicurezza

- **SSRF**: solo http/https, blocco IP privati/loopback/metadata, ri-validazione a ogni redirect, blocklist domini.
- **Prompt injection**: il contenuto web è racchiuso in `<external_web_content>` e trattato come dato, mai come istruzione.
- **Secrets**: solo via `.env` (git-ignorato); mai nei log; l'API `/api/settings` espone solo `set`/`unset`.

## Limiti noti

- Laya zero-shot è debole su choice/score in italiano (vedi benchmark): usarlo lì richiede fine-tuning.
- DuckDuckGo keyless è best-effort (niente SLA; per uso intenso meglio Tavily free tier).
- PDF export usa font core (copertura latin-1): caratteri extra-script sostituiti.
