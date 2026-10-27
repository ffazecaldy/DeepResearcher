"""Confronto prima/dopo sul run rigenerato: verifica degli 8 difetti del brief."""
import json
import re
import sqlite3
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8766"
RID = sys.argv[1] if len(sys.argv) > 1 else "run_1793127481_c199bb6d"


def wait_done(max_s=1500):
    t0 = time.time()
    while time.time() - t0 < max_s:
        d = json.load(urllib.request.urlopen(f"{BASE}/api/runs/{RID}"))
        st = d["run"]["status"]
        if st != "running":
            return st, d
        time.sleep(20)
    return "timeout", None


def main():
    st, d = wait_done()
    print(f"STATO FINALE: {st}")
    conn = sqlite3.connect("data/deep_researcher.db")
    conn.row_factory = sqlite3.Row

    rep = conn.execute("SELECT markdown FROM reports WHERE run_id=?", (RID,)).fetchone()
    md = rep["markdown"] if rep else ""
    body = md.split("## Fonti", 1)[0]
    fonti = md.split("## Fonti", 1)[1] if "## Fonti" in md else ""

    print("\n=== DIFETTO 1+2: marker [n] vs bibliografia ===")
    inline = {int(m) for m in re.findall(r"\[(\d+)\]", body)}
    bib = {int(m.group(1)) for line in fonti.splitlines()
           if (m := re.match(r"^(\d+)\.\s", line))}
    print(f"  marker nel testo: {sorted(inline)}")
    print(f"  voci bibliografia: {sorted(bib)}")
    print(f"  dangling (marker senza voce): {sorted(inline - bib) or 'NESSUNO'}")
    print(f"  voci senza marker: {sorted(bib - inline) or 'nessuna'}")

    print("\n=== DIFETTO 3: identificatori interni nel testo ===")
    leaks = re.findall(r"\[(?:ev|clm|src)_[^\]]*\]|\b(?:ev|clm|src)_[0-9a-f]{8,}\b", body)
    print(f"  occorrenze: {len(leaks)}", leaks[:5] if leaks else "")

    print("\n=== DIFETTO 4: GISSI (attribuzione) ===")
    g = [ln for ln in body.splitlines() if "GISSI" in ln.upper()]
    for ln in g:
        hedged = "secondo" in ln.lower() or "hedged" in ln.lower()
        print(f"  {ln[:100]}")
        print(f"    attribuzione 'secondo la fonte': {'SI' if hedged else 'NO (voce propria)'}")

    print("\n=== DIFETTO 5: data PubMed 33201262 ===")
    for r in conn.execute("SELECT id, url, published_at FROM sources WHERE run_id=? AND url LIKE '%33201262%'", (RID,)):
        print(f"  {r['id']} | {r['url'][:60]} | published_at: {r['published_at']}")
    bad = conn.execute("SELECT COUNT(*) FROM sources WHERE run_id=? AND url LIKE '%33201262%' AND published_at LIKE '1995%'", (RID,)).fetchone()[0]
    print(f"  '1995' residuo: {bad}")

    print("\n=== DIFETTO 6: duplicati per identita ===")
    dup = conn.execute("""SELECT COUNT(*) FROM sources WHERE run_id=? AND identity_key IN
        (SELECT identity_key FROM sources WHERE run_id=? AND identity_key != ''
         GROUP BY identity_key HAVING COUNT(*) > 1)""",
        (RID, RID)).fetchone()[0]
    n_id = conn.execute("SELECT COUNT(*) FROM sources WHERE run_id=? AND identity_key != ''", (RID,)).fetchone()[0]
    print(f"  fonti con identity_key: {n_id} | in gruppi duplicati: {dup}")
    dup_ev = conn.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND type='source_duplicate'", (RID,)).fetchone()[0]
    print(f"  eventi source_duplicate emessi: {dup_ev}")

    print("\n=== DIFETTO 7: guardia popolazione (ADHD->attenzione) ===")
    weakened = conn.execute("""SELECT reason FROM verifications WHERE run_id=? AND verdict='WEAKENED'""", (RID,)).fetchall()
    pop = [w["reason"] for w in weakened if "population" in (w["reason"] or "").lower() or "popolazione" in (w["reason"] or "").lower()]
    print(f"  WEAKENED totali: {len(weakened)} | per popolazione: {len(pop)}")
    for p_ in pop[:3]:
        print("   -", p_[:90])

    print("\n=== DIFETTO 8: 'contrastanti' senza base ===")
    claims_contrast = [ln for ln in body.splitlines()
                       if re.search(r"contrastant|contraddittor", ln, re.I)]
    ver = conn.execute("SELECT reason FROM verifications WHERE run_id=? AND reason LIKE '%mixed%'", (RID,)).fetchall()
    print(f"  frasi con 'contrastanti': {len(claims_contrast)} | verifiche mixed reali: {len(ver)}")
    if claims_contrast and not ver:
        print("  ATTENZIONE: 'contrastanti' senza mixed reali")
    elif not claims_contrast:
        print("  nessuna meta-affermazione 'contrastanti' nel report")

    print("\n=== COERENZA (endpoint) ===")
    try:
        coh = json.load(urllib.request.urlopen(f"{BASE}/api/runs/{RID}/coherence"))
        print(f"  passed: {coh['passed']} | issues: {len(coh['issues'])} | frasi: {coh['checked_sentences']}")
        for i in coh["issues"][:5]:
            print(f"   [{i['type']}] {i['sentence'][:70]}")
    except Exception as exc:
        print("  coerenza non disponibile:", exc)

    print("\n=== BIBLIOGRAFIA (prime 6 voci) ===")
    for line in fonti.splitlines()[:8]:
        if line.strip():
            print("  " + line[:110])


if __name__ == "__main__":
    main()
