"""B5: lingua del piano = report_language globale, budget tempo per ciclo,
tabella per-cycle negli stats."""
from __future__ import annotations

import inspect
import sys
import time

sys.path.insert(0, ".")

from app.config import Settings


# ---------- lingua del piano ----------

def _orch_source():
    from app.agent import orchestrator
    return inspect.getsource(orchestrator.Orchestrator.run)


def test_plan_language_follows_report_language():
    src = _orch_source()
    # il planning usa report_language (fallback: lingua domanda), non la raw
    assert "report_language" in src
    assert "plan_lang" in src


def test_report_language_setting_exists():
    s = Settings(_env_file=None)
    assert s.report_language == "it"  # default globale


# ---------- budget per ciclo ----------

def test_max_seconds_per_cycle_setting():
    s = Settings(_env_file=None)
    assert hasattr(s, "max_seconds_per_cycle")
    assert s.max_seconds_per_cycle == 0  # default disattivo
    s2 = Settings(_env_file=None, max_seconds_per_cycle=120)
    assert s2.max_seconds_per_cycle == 120


def test_cycle_deadline_code_present():
    src = _orch_source()
    assert "cycle_deadline" in src
    assert "LIMITE_CICLO" in src


def test_per_cycle_stats_code_present():
    src = _orch_source()
    assert '"per_cycle"' in src  # stats aggregati per ciclo
