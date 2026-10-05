"""B4: ruoli del ciclo (cycle_started.role), scala di novità, snowballing link."""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from app.agent.search_state import SearchState, cycle_role, novelty_score
from app.agent.snowball import extract_links, SnowballState


# ---------- ruoli del ciclo ----------

def test_cycle_role_mapping():
    assert cycle_role(1) == "esplorazione"
    assert cycle_role(2) == "approfondimento"
    assert cycle_role(3) == "verifica"
    assert cycle_role(4) == "verifica"
    assert cycle_role(5) == "verifica"


def test_cycle_role_invalid_cycle():
    assert cycle_role(0) == "esplorazione"
    assert cycle_role(-1) == "esplorazione"


# ---------- scala di novità ----------

def test_novelty_new_domain_beats_seen_domain():
    st = SearchState()
    st.add_url("https://a.example/x")  # dominio a visto 1 volta
    assert novelty_score("https://b.example/y", st) == 2   # dominio nuovo
    assert novelty_score("https://a.example/z", st) == 1   # dominio visto, URL nuovo
    # URL già visto -> 0 (viene comunque scartato a monte)
    st.add_url("https://a.example/z")
    assert novelty_score("https://a.example/z", st) == 0


def test_novelty_ordering_stable_for_unknown_state():
    # senza stato (SearchState vuoto) ogni URL è dominio-nuovo
    st = SearchState()
    assert novelty_score("https://qualunque.example/", st) == 2


# ---------- snowballing ----------

def test_extract_links_basic():
    html = """
    <p>Testo <a href="https://ok.example/a">link ok</a>
    <a href="/relativo/pagina">relativo</a>
    <a href="javascript:void(0)">js</a>
    <a href="mailto:a@b.c">mail</a>
    <a href="#ancora">ancora</a></p>
    """
    links = extract_links(html, base_url="https://ok.example/")
    assert "https://ok.example/a" in links
    assert "https://ok.example/relativo/pagina" in links
    assert all(not u.startswith(("javascript:", "mailto:")) for u in links)
    assert all(not u.startswith("#") for u in links)


def test_snowball_state_caps_and_dedup():
    st = SnowballState(max_total=3)
    assert st.propose("https://one.example/a") is True
    assert st.propose("https://one.example/a") is False      # dedup
    assert st.propose("https://two.example/b") is True
    assert st.propose("https://three.example/c") is True
    assert st.propose("https://four.example/d") is False     # cap raggiunto
    assert len(st.candidates) == 3


def test_snowball_state_filters_private_urls():
    st = SnowballState()
    assert st.propose("not-a-url") is False
    assert st.propose("ftp://files.example/x") is False
    assert st.propose("https://ok.example/yes") is True
