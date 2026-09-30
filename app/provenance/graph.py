"""Provenance graph builder: pure functions over persisted run rows."""
from __future__ import annotations

import json
from typing import Any

from app.security import normalize_for_dedupe


def _node(nid: str, kind: str, label: str) -> dict[str, str]:
    return {"id": nid, "kind": kind, "label": (label or "")[:160]}


def _edge(src: str, dst: str, kind: str) -> dict[str, str]:
    return {"from": src, "to": dst, "kind": kind}


def build_graph(run_row: dict[str, Any],
                subquestions: list[dict[str, Any]],
                queries: list[dict[str, Any]],
                search_results: list[dict[str, Any]],
                sources: list[dict[str, Any]],
                chunks: list[dict[str, Any]],
                evidences: list[dict[str, Any]],
                claims: list[dict[str, Any]],
                verifications: list[dict[str, Any]],
                decisions: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    """Build the full question->citation provenance graph for one run."""
    nodes: list[dict[str, str]] = []
    edges: list[dict[str, str]] = []
    known: set[str] = set()
    rid = run_row.get("id", "run")
    nodes.append(_node(rid, "run", str(run_row.get("question", ""))))
    known.add(rid)

    sub_by_id: dict[str, dict[str, Any]] = {}
    for sq in subquestions:
        sid = str(sq.get("id"))
        sub_by_id[sid] = sq
        nodes.append(_node(sid, "subquestion", str(sq.get("text", ""))))
        known.add(sid)
        edges.append(_edge(rid, sid, "has_subquestion"))

    q_by_id: dict[str, dict[str, Any]] = {}
    for q in queries:
        qid = str(q.get("id"))
        q_by_id[qid] = q
        nodes.append(_node(qid, "query", str(q.get("text", ""))))
        known.add(qid)
        parent = q.get("subquestion_id")
        edges.append(_edge(parent if parent in known else rid, qid, "issued"))

    sr_by_url: dict[str, str] = {}
    for r in search_results:
        nid = f"sr:{r.get('id')}"
        url = str(r.get("url_normalized") or r.get("url") or "")
        nodes.append(_node(nid, "search_result", url))
        known.add(nid)
        parent = r.get("query_id")
        edges.append(_edge(parent if parent in known else rid, nid, "found"))
        if url:
            sr_by_url.setdefault(normalize_for_dedupe(url), nid)

    src_by_id: dict[str, dict[str, Any]] = {}
    for s in sources:
        sid = str(s.get("id"))
        src_by_id[sid] = s
        nodes.append(_node(sid, "source",
                           str(s.get("url_final") or s.get("url") or "")))
        known.add(sid)
        url_key = normalize_for_dedupe(str(s.get("url_final") or s.get("url") or ""))
        parent = sr_by_url.get(url_key)
        edges.append(_edge(parent if parent in known else rid, sid, "fetched"))

    chunk_by_id: dict[str, dict[str, Any]] = {}
    for c in chunks:
        cid = str(c.get("id"))
        chunk_by_id[cid] = c
        nodes.append(_node(cid, "chunk", str(c.get("text", ""))[:120]))
        known.add(cid)
        parent = str(c.get("source_id"))
        edges.append(_edge(parent if parent in known else rid, cid, "chunk_of"))

    ev_by_id: dict[str, dict[str, Any]] = {}
    for e in evidences:
        eid = str(e.get("id"))
        ev_by_id[eid] = e
        nodes.append(_node(eid, "evidence", str(e.get("claim", ""))))
        known.add(eid)
        parent = str(e.get("chunk_id") or "")
        if parent not in known:
            parent = str(e.get("source_id") or "")
        edges.append(_edge(parent if parent in known else rid, eid, "extracted"))

    for cl in claims:
        cid = str(cl.get("id"))
        nodes.append(_node(cid, "claim", str(cl.get("text", ""))))
        known.add(cid)
        raw = cl.get("evidence_ids_json") or "[]"
        try:
            ev_ids = json.loads(raw)
        except ValueError:
            ev_ids = []
        for eid in ev_ids:
            eid = str(eid)
            edges.append(_edge(eid if eid in known else rid, cid, "supports"))

    for v in verifications:
        vid = f"ver:{v.get('id')}"
        nodes.append(_node(vid, "verification", str(v.get("verdict", ""))))
        known.add(vid)
        parent = str(v.get("claim_id") or "")
        edges.append(_edge(parent if parent in known else rid, vid, "verified_by"))

    for d in decisions:
        did = f"dec:{d.get('id')}"
        label = f"{d.get('decision_type')}={d.get('value_json')} [{d.get('engine')}]"
        nodes.append(_node(did, "decision", label))
        known.add(did)
        subject = str(d.get("subject_id") or "")
        if subject in known:
            edges.append(_edge(did, subject, "decided_on"))
        else:
            edges.append(_edge(rid, did, "decision"))

    return {"nodes": nodes, "edges": edges}
