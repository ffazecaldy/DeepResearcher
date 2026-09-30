"""Benchmark: LayaDecisionEngine vs LLMDecisionEngine on ~20 labeled cases.

Run:  .venv/Scripts/python.exe tests/benchmarks/bench_decisions.py [--with-llm]
Results: tests/benchmarks/results_laya.json (+ results_llm.json when enabled).
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

from app.decision.laya import LayaDecisionEngine
from app.models import DecisionKind, DecisionSpec

CASES = [
    # (question, document, is_relevant_label, source_type_label, quality_label 0-2)
    ("Chi ha brevettato il telefono?",
     "Alexander Graham Bell fu il primo a ottenere il brevetto del telefono, "
     "nel 1876, con la richiesta n. 174465 depositata presso l'ufficio brevetti USA.",
     True, "official", 2),
    ("Chi ha brevettato il telefono?",
     "La ricetta della carbonara originale prevede guanciale, pecorino e uova. "
     "I tempi di cottura della pasta sono fondamentali per la cremosità.",
     False, "other", 1),
    ("Chi ha brevettato il telefono?",
     "Antonio Meucci sviluppò un dispositivo di trasmissione vocale, il telettrofono, "
     "anni prima di Bell, ma non poté mantenere attivo il brevetto provvisorio.",
     True, "paper", 2),
    ("Chi ha brevettato il telefono?",
     "Il campionato di calcio si è concluso con la vittoria per 2-1. "
     "Il gol decisivo è arrivato al 89º minuto davanti a 40.000 spettatori.",
     False, "news", 1),
    ("Qual è la capitale dell'Australia?",
     "Canberra è la capitale dell'Australia: fu scelta come compromesso tra "
     "Sydney e Melbourne e divenne sede del parlamento federale nel 1927.",
     True, "official", 2),
    ("Qual è la capitale dell'Australia?",
     "Il koala vive negli eucaliptus australiani e dorme fino a 20 ore al giorno. "
     "La sua dieta è estremamente specializzata.",
     False, "other", 0),
    ("Qual è la capitale dell'Australia?",
     "Molti credono che sia Sydney, ma la sede del governo è Canberra. "
     "I giornali turistici spiegano spesso l'equivoco ai visitatori.",
     True, "news", 1),
    ("Come funziona la fotosintesi clorofilliana?",
     "Nella fotosintesi le piante convertono anidride carbonica e acqua in glucosio "
     "e ossigeno usando l'energia luminosa assorbita dalla clorofilla nei cloroplasti.",
     True, "paper", 2),
    ("Come funziona la fotosintesi clorofilliana?",
     "Il fotovoltaico converte la luce solare in elettricità tramite celle di silicio. "
     "Il rendimento medio dei pannelli residenziali è intorno al 20 per cento.",
     False, "news", 1),
    ("Come funziona la fotosintesi clorofilliana?",
     "Ho provato a coltivare basilico sul balcone: con più ore di luce diretta "
     "le foglie sono diventate più profumate e la pianta più folta.",
     False, "forum", 0),
    ("Chi ha scritto la Divina Commedia?",
     "La Divina Commedia fu scritta da Dante Alighieri nei primi anni del "
     "Trecento ed è divisa in Inferno, Purgatorio e Paradiso.",
     True, "paper", 2),
    ("Chi ha scritto la Divina Commedia?",
     "I Pooh sono una band italiana nata negli anni Sessanta. "
     "Il loro tour annuale tocca decine di piazze.",
     False, "news", 0),
    ("Chi ha scritto la Divina Commedia?",
     "Nella biblioteca comunale ho trovato un'edizione annotate della Commedia: "
     "chi la scrisse la denominò semplicemente 'Commedia'; l'aggettivo 'Divina' "
     "fu aggiunto da posteriori.",
     True, "forum", 1),
    ("Quale azienda produce il caffè?",
     "Error doc with weird content about laptops: Il processore del nuovo laptop "
     "ha 12 core e 32 GB di RAM, ottimo per il video editing professionale.",
     False, "other", 0),
    ("Come si cura l'ipertensione?",
     "Il trattamento dell'ipertensione arteriosa combina modifiche dello stile di vita "
     "e farmaci come ACE-inibitori, diuretici e betablocanti secondo le linee guida.",
     True, "official", 2),
    ("Come si cura l'ipertensione?",
     "La mia nonna giura che il succo di barbabietola le abbia abbassato la pressione. "
     "Qualcuno ha esperienze simili con integratori alimentari?",
     False, "forum", 0),
    ("Come si cura l'ipertensione?",
     "Uno studio randomizzato mostra che la riduzione del sodio a meno di 5 g/die "
     "abbassa la pressione sistolica di 4-5 mmHg in media negli ipertesi.",
     True, "paper", 2),
    ("Quando è caduto il Muro di Berlino?",
     "Il Muro di Berlino cadde la notte tra il 9 e il 10 novembre 1989, "
     "quando la DDR aprì i valichi di frontiera dopo settimane di proteste.",
     True, "news", 2),
    ("Quando è caduto il Muro di Berlino?",
     "Berlino oggi è la capitale tedesca e conta oltre 3,5 milioni di abitanti. "
     "Il costo degli affitti è salito rapidamente nell'ultimo decennio.",
     False, "news", 1),
    ("Quando è caduto il Muro di Berlino?",
     "Il muro cinese è la struttura difensiva più lunga mai costruita. "
     "La sua costruzione durò secoli sotto dinastie diverse.",
     False, "other", 0),
]

SPECS = [
    DecisionSpec(name="is_relevant", kind=DecisionKind.BINARY,
                 question="Does this document help answer the question?"),
    DecisionSpec(name="source_type", kind=DecisionKind.CHOICE,
                 question="What kind of source is this?",
                 options={"official": "institutions, standards bodies",
                          "paper": "research, studies, encyclopedic depth",
                          "news": "journalism, media",
                          "forum": "community discussions, personal experiences",
                          "other": "anything else"}),
    DecisionSpec(name="quality", kind=DecisionKind.SCORE,
                 question="Overall reliability of this source for the question?",
                 scale=["low", "medium", "high"]),
]

OUT = Path("tests/benchmarks")


def _state(question: str, doc: str) -> str:
    return f"QUESTION: {question}\n\nDOCUMENT:\n{doc}"


async def bench_engine(engine, label: str) -> dict:
    rows = []
    lat: list[float] = []
    t_all = time.perf_counter()
    for i, (q, doc, rel_lab, type_lab, qual_lab) in enumerate(CASES):
        results = await engine.decide_many(
            SPECS, _state(q, doc), subject_kind="bench", subject_id=str(i))
        got = {r.name: r for r in results}
        lat.extend(r.latency_ms for r in results)
        rows.append({
            "case": i,
            "is_relevant": {"expected": rel_lab,
                            "got": got["is_relevant"].value,
                            "ok": bool(got["is_relevant"].value) == rel_lab,
                            "abstained": got["is_relevant"].abstained},
            "source_type": {"expected": type_lab,
                            "got": got["source_type"].value,
                            "ok": got["source_type"].value == type_lab,
                            "abstained": got["source_type"].abstained},
            "quality": {"expected": qual_lab,
                        "got": got["quality"].value,
                        "ok": got["quality"].value == qual_lab,
                        "abstained": got["quality"].abstained},
        })
    wall = time.perf_counter() - t_all

    def acc(key):
        oks = [r[key]["ok"] for r in rows]
        abst = sum(1 for r in rows if r[key]["abstained"])
        return {"accuracy": sum(oks) / len(oks), "abstentions": abst}

    return {
        "engine": label,
        "cases": len(CASES),
        "decisions": len(CASES) * 3,
        "accuracy_by_decision": {k: acc(k) for k in
                                 ("is_relevant", "source_type", "quality")},
        "wall_seconds": round(wall, 2),
        "latency_ms_median": round(statistics.median(lat), 1),
        "rows": rows,
    }


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with_llm = "--with-llm" in sys.argv

    laya_eng = LayaDecisionEngine(min_confidence=0.35)
    report = await bench_engine(laya_eng, "laya")
    (OUT / "results_laya.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), "utf-8")
    print("=== LAYA ===")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"},
                     indent=1, ensure_ascii=False))

    if with_llm:
        from app.clients.llm.base import build_llm_client
        from app.config import load_settings
        from app.decision.llm_fallback import LLMDecisionEngine
        llm = build_llm_client(load_settings())
        llm_eng = LLMDecisionEngine(llm)
        rep2 = await bench_engine(llm_eng, "llm")
        (OUT / "results_llm.json").write_text(
            json.dumps(rep2, indent=1, ensure_ascii=False), "utf-8")
        print("=== LLM ===")
        print(json.dumps({k: v for k, v in rep2.items() if k != "rows"},
                         indent=1, ensure_ascii=False))
        await llm.aclose()


if __name__ == "__main__":
    asyncio.run(main())
