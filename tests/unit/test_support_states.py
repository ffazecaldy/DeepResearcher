"""Regression: stati di verifica espliciti (support) + guardia anti-bias.

- verifier: support=contradicted degrada VERIFIED -> WEAKENED
- convenzione prefisso "[support:X] motivo" su verdict_reason
- writer: riquadro "> Stati delle affermazioni: ..." solo se support_stats
- planner: la guardia anti-bias arriva nel prompt utente (ciclo 1)
"""
from __future__ import annotations

import json
from pathlib import Path

from app.agent.planner import Planner, _ANTIBIAS_RULE
from app.agent.verifier import Verifier
from app.agent.writer import Writer
from app.models import DraftReport, Evidence, ReportClaim, SubQuestion
from tests.fakes import FakeLLMClient


def _verifier_prompts(tmp_path: Path) -> Path:
    d = tmp_path / "prompts"
    d.mkdir(exist_ok=True)
    (d / "verifier.txt").write_text(
        "SYS\n---SYSTEM---\nQ {question} C {claim} EV {evidence_json}\n",
        encoding="utf-8")
    return d


def _ev(eid: str, quote: str) -> Evidence:
    return Evidence(evidence_id=eid, source_id="src1", chunk_id="chk1",
                    quote=quote, claim="claim base", run_id="r1")


def _draft(*claims: ReportClaim) -> DraftReport:
    return DraftReport(title="T", markdown=" ".join(c.text for c in claims),
                       claims=list(claims))


async def test_contradicted_degrades_verified(tmp_path):
    """support=contradicted con verdict VERIFIED -> WEAKENED."""
    quote = "Gli studi non mostrano alcun legame tra i due fenomeni."
    claim = ReportClaim(text="Gli studi mostrano un legame tra i due fenomeni.",
                        citation_no=1, evidence_ids=["ev1"])
    fake = FakeLLMClient([json.dumps({
        "verdict": "VERIFIED", "support": "contradicted", "reason": "ok"})])
    v = Verifier(fake, prompts_dir=_verifier_prompts(tmp_path))

    out = await v.verify("q", _draft(claim), {"ev1": _ev("ev1", quote)}, {})

    c = out.claims[0]
    assert c.verdict == "WEAKENED"
    assert "evidenza contraddice" in c.verdict_reason
    assert out.stats.number_failed == 1  # WEAKENED conta come check fallito


async def test_support_prefix_and_default(tmp_path):
    """Prefisso [support:X] sempre presente; senza campo -> insufficient."""
    quote = "Il telefono fu brevettato da Bell."
    claim_ok = ReportClaim(text="Il telefono fu brevettato da Bell.",
                           citation_no=1, evidence_ids=["ev1"])
    claim_missing = ReportClaim(text="Bell lavorò al telefono.",
                                citation_no=2, evidence_ids=["ev1"])
    fake = FakeLLMClient([
        json.dumps({"verdict": "VERIFIED", "support": "supported",
                    "reason": "citazione diretta"}),
        json.dumps({"verdict": "WEAKENED", "reason": "paragrafo vago"}),
    ])
    v = Verifier(fake, prompts_dir=_verifier_prompts(tmp_path))

    out = await v.verify("q", _draft(claim_ok, claim_missing),
                         {"ev1": _ev("ev1", quote)}, {})

    assert out.claims[0].verdict_reason.startswith("[support:supported] ")
    assert out.claims[1].verdict_reason.startswith("[support:insufficient] ")
    assert out.claims[1].verdict == "WEAKENED"  # default non tocca il verdict


async def test_writer_support_stats_box(tmp_path):
    """Il riquadro stati appare solo quando support_stats e' non vuoto."""
    d = tmp_path / "wprompts"
    d.mkdir(exist_ok=True)
    (d / "writer.txt").write_text(
        "SYS\n---SYSTEM---\nQ {question} L {language} EV {evidence_json} "
        "SQ {subquestions_json}\n",
        encoding="utf-8")

    def _report_resp(system, user, *, force_json=False):
        return json.dumps({"title": "T", "markdown": "Fatto uno [1].",
                           "claims": [{"text": "Fatto uno.",
                                       "evidence_ids": ["ev1"]}]})

    w = Writer(FakeLLMClient(_report_resp), prompts_dir=d)
    subs = [SubQuestion(subquestion_id="sq1", text="s", idx=1)]
    evs = [_ev("ev1", "quote")]

    with_stats = await w.write("domanda", "it", subs, evs,
                               support_stats={"supported": 3, "contradicted": 1,
                                              "insufficient": 2, "mixed": 0})
    assert ("Stati delle affermazioni: 3 supportate, 1 contraddette, "
            "2 evidenze insufficienti, 0 contrastate") in with_stats.markdown

    empty = await w.write("domanda", "it", subs, evs, support_stats={})
    none_stats = await w.write("domanda", "it", subs, evs)
    assert "Stati delle affermazioni" not in empty.markdown
    assert "Stati delle affermazioni" not in none_stats.markdown


async def test_planner_prompt_carries_antibias_guard(tmp_path):
    """La guardia anti-bias e' nel prompt utente del planner (ciclo 1)."""
    d = tmp_path / "pprompts"
    d.mkdir(exist_ok=True)
    (d / "planner.txt").write_text(
        "SYS\n---SYSTEM---\nQ {question} L {language} M {max_subquestions}\n",
        encoding="utf-8")
    plan_json = json.dumps({
        "title": "T", "language": "it",
        "subquestions": [{"subquestion_id": "s1", "text": "sub", "idx": 1}],
        "queries": [{"query_id": "q1", "subquestion_id": "s1",
                     "text": "query controevidenza"}]})
    fake = FakeLLMClient([plan_json])
    planner = Planner(fake, prompts_dir=d)

    await planner.make_plan("domanda", "it")

    assert "ANTI-BIAS" in fake.last_user
    assert "contrario del presupposto" in fake.last_user
    assert "controevidenza" in fake.last_user
    assert _ANTIBIAS_RULE in fake.last_user
