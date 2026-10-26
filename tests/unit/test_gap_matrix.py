"""Regression: build_gap_matrix su caso reale + log dentro compute_coverage.

Scenario: domanda sulla camminata veloce e pressione arteriosa.
- sq1 coperta (3 evidenze, 2 domini indipendenti, una "paper") MA con una
  contraddizione aperta che la riguarda -> next_step smentite/aggiornamenti
- sq2 parziale (1 sola fonte indipendente, di tipo "forum", nessuna primaria)
- sq3 non coperta (nessuna fonte)
"""
from __future__ import annotations

import json
import logging

from app.agent.gap_checker import build_gap_matrix, compute_coverage
from app.models import Evidence, SubQuestion

CONTRADICTION = ("Uno studio riporta -5 mmHg sulla pressione arteriosa, "
                 "un altro nessun effetto")


def _subs() -> list[SubQuestion]:
    return [
        SubQuestion(subquestion_id="sq1",
                    text="Riduzione della pressione arteriosa dopo dodici "
                         "settimane?", idx=1),
        SubQuestion(subquestion_id="sq2",
                    text="Dosi raccomandate dall'OMS per gli adulti?", idx=2),
        SubQuestion(subquestion_id="sq3",
                    text="Quale aderenza al trattamento?", idx=3),
    ]


def _ev(eid: str, sid: str, source: str, source_type: str | None = None) -> Evidence:
    return Evidence(evidence_id=eid, run_id="r1", source_id=source,
                    chunk_id="chk", quote="q", claim="c",
                    subquestion_id=sid, source_type=source_type)


def _evidences() -> list[Evidence]:
    return [
        # sq1: 3 evidenze ma solo 2 DOMINI indipendenti; una primaria ("paper")
        _ev("ev1", "sq1", "src_a", "paper"),
        _ev("ev2", "sq1", "src_b", "news"),
        _ev("ev3", "sq1", "src_a2", "paper"),
        # sq2: una sola fonte, non primaria
        _ev("ev4", "sq2", "src_c", "forum"),
    ]


DOMAIN_OF = {
    "src_a": "medjournal-example.org", "src_b": "salute-example.it",
    "src_a2": "medjournal-example.org", "src_c": "forum-example.com",
}


def test_matrix_real_case():
    matrix = build_gap_matrix(_subs(), _evidences(), DOMAIN_OF,
                              [CONTRADICTION])

    assert isinstance(matrix, list) and len(matrix) == 3
    assert all(set(m) == {"subquestion_id", "text", "evidence_count",
                          "distinct_domains", "status", "problems",
                          "next_step"} for m in matrix)
    by_id = {m["subquestion_id"]: m for m in matrix}

    # sq1: coperta ma con contraddizione aperta (match per 2 token significativi)
    sq1 = by_id["sq1"]
    assert sq1["evidence_count"] == 3 and sq1["distinct_domains"] == 2
    assert sq1["status"] == "coperta"
    assert "contraddizione aperta" in sq1["problems"]
    assert "nessuna fonte primaria" not in sq1["problems"]  # c'e' "paper"
    assert sq1["next_step"] == "cerca smentite o aggiornamenti"

    # sq2: una sola fonte indipendente e nessuna primaria -> entrambi i problemi
    sq2 = by_id["sq2"]
    assert sq2["evidence_count"] == 1 and sq2["distinct_domains"] == 1
    assert sq2["status"] == "parziale"
    assert "una sola fonte indipendente" in sq2["problems"]
    assert "nessuna fonte primaria" in sq2["problems"]
    assert "contraddizione aperta" not in sq2["problems"]  # non la riguarda
    assert sq2["next_step"] == "cerca conferma su una seconda fonte indipendente"

    # sq3: zero fonti
    sq3 = by_id["sq3"]
    assert sq3["evidence_count"] == 0 and sq3["distinct_domains"] == 0
    assert sq3["status"] == "non_coperta"
    assert sq3["problems"] == ["nessuna fonte"]
    assert sq3["next_step"] == ("cerca una prima fonte indipendente "
                                "(istituzionale o primaria)")

    # pura: chiamata ripetuta -> stesso risultato, input non mutati
    evs = _evidences()
    m2 = build_gap_matrix(_subs(), evs, DOMAIN_OF, [CONTRADICTION])
    assert m2 == matrix and len(evs) == 4

    # serializzabile per il log
    json.dumps(matrix, ensure_ascii=False)


def test_unknown_source_type_does_not_invent_primary_problem():
    # 2 evidenze sullo STESSO dominio, source_type ignoto (None): il problema
    # "nessuna fonte primaria" NON va inventato, resta solo la fonte unica
    subs = [SubQuestion(subquestion_id="sq9", text="Aderenza media?", idx=1)]
    evs = [_ev("e1", "sq9", "s1"), _ev("e2", "sq9", "s2")]
    matrix = build_gap_matrix(subs, evs, {"s1": "d1.it", "s2": "d1.it"})
    assert matrix[0]["status"] == "parziale"
    assert matrix[0]["problems"] == ["una sola fonte indipendente"]
    assert matrix[0]["next_step"] == ("cerca conferma su una seconda "
                                      "fonte indipendente")


def test_compute_coverage_logs_matrix_and_keeps_contract(caplog):
    # la firma/contratto di compute_coverage NON cambia
    complete, status = compute_coverage(_subs(), _evidences(), DOMAIN_OF)
    assert complete is False  # sq3 senza fonti
    assert status == {"sq1": "coperta", "sq2": "parziale", "sq3": "non_coperta"}

    # la matrice e' arricchimento del LOG, non del ritorno (contraddizioni non
    # sono note a compute_coverage -> problemi di sola copertura nel suo log;
    # la matrice COMPLETA con le contraddizioni e' loggata da GapChecker.check)
    with caplog.at_level(logging.INFO, logger="app.agent.gap_checker"):
        compute_coverage(_subs(), _evidences(), DOMAIN_OF)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "matrice lacune" in logged
    assert "sq1" in logged and "cerca una prima fonte indipendente" in logged
    assert "una sola fonte indipendente" in logged
    # e la matrice completa (con contraddizione) esiste come funzione pura
    full = build_gap_matrix(_subs(), _evidences(), DOMAIN_OF, [CONTRADICTION])
    assert full[0]["next_step"] == "cerca smentite o aggiornamenti"
