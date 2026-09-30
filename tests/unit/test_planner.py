"""Unit tests for app.agent.planner."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agent.planner import Planner, PlannerError
from tests.fakes import FakeLLMClient

VALID = {
    "title": "Piano",
    "language": "it",
    "subquestions": [
        {"subquestion_id": "SQ_MODEL_1", "text": "Chi ha inventato il telefono?"},
        {"subquestion_id": "SQ_MODEL_2", "text": "Quando fu brevettato?"},
    ],
    "queries": [
        {"subquestion_id": "SQ_MODEL_1", "text": "inventore telefono storia"},
        {"subquestion_id": "SQ_MODEL_1", "text": "Inventore  TELEFONO  storia"},
        {"subquestion_id": "SQ_MODEL_2", "text": "brevetto telefono 1876"},
    ],
}


def _tmp_prompts(tmp_path: Path, system: str, user: str) -> Path:
    d = tmp_path / "prompts"
    d.mkdir(exist_ok=True)
    (d / "planner.txt").write_text(f"{system}\n---SYSTEM---\n{user}\n",
                                   encoding="utf-8")
    return d


async def test_happy_path_dedup_and_deterministic_ids(tmp_path):
    prompts = _tmp_prompts(tmp_path, "SYS", "Q: {question} L: {language} "
                                            "N: {max_subquestions}")
    fake = FakeLLMClient([json.dumps(VALID)])
    planner = Planner(fake, prompts_dir=prompts)

    plan = await planner.make_plan("Storia del telefono", "it",
                                   max_subquestions=3)

    assert [s.subquestion_id for s in plan.subquestions] == ["sq1", "sq2"]
    assert plan.subquestions[0].idx == 1
    # near-duplicate (Jaccard > 0.8) dropped; ids rewritten deterministically
    assert [q.query_id for q in plan.queries] == ["q1", "q2"]
    assert all(q.subquestion_id in {"sq1", "sq2"} for q in plan.queries)
    assert plan.queries[0].text == "inventore telefono storia"
    assert plan.language == "it"

    # prompt placeholders formatted; force_json requested
    user = fake.last_user
    assert "Storia del telefono" in user and "it" in user
    assert fake.calls[0]["force_json"] is True
    assert fake.calls[0]["system"] == "SYS"


async def test_invalid_json_retry_then_error(tmp_path):
    prompts = _tmp_prompts(tmp_path, "SYS", "{question}")
    fake = FakeLLMClient(["not json at all", "still ::: broken {"])
    planner = Planner(fake, prompts_dir=prompts)

    with pytest.raises(PlannerError):
        await planner.make_plan("domanda", "it")

    assert fake.call_count == 2  # 1 original + 1 repair retry
    assert "JSON" in fake.last_user or "json" in fake.last_user


async def test_json_fence_stripped_and_retry_succeeds(tmp_path):
    prompts = _tmp_prompts(tmp_path, "SYS", "{question}")
    fenced = "```json\n" + json.dumps(VALID) + "\n```"
    fake = FakeLLMClient(["oops", fenced])
    planner = Planner(fake, prompts_dir=prompts)

    plan = await planner.make_plan("domanda", "it")
    assert len(plan.subquestions) == 2
    assert fake.call_count == 2


async def test_second_attempt_valid_returns_plan_without_error(tmp_path):
    prompts = _tmp_prompts(tmp_path, "SYS", "{question}")
    fake = FakeLLMClient(["garbage", json.dumps(VALID)])
    planner = Planner(fake, prompts_dir=prompts)
    plan = await planner.make_plan("domanda", "it")
    assert plan.title == "Piano"
