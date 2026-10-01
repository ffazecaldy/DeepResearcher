"""Estrae la diagnosi dal DB per il run approfondita (FASE A)."""
import sqlite3

conn = sqlite3.connect("data/deep_researcher.db")
conn.row_factory = sqlite3.Row
r = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
rid = r["id"]
print("RUN:", rid, "|", r["status"], "| depth:", r["depth"],
      "| err:", (r["error"] or "nessuno")[:150])

q = lambda s, *p: [dict(x) for x in conn.execute(s, p).fetchall()]
q1 = lambda s, p=None: [dict(x) for x in conn.execute(s, p or ()).fetchall()]
# note: passa SEMPRE una tupla singola: q("...=?", (rid,))

print("\n=== QUERY PER CICLO ===")
for row in q("SELECT cycle, COUNT(*) n, GROUP_CONCAT(text, ' || ') txt "
             "FROM queries WHERE run_id=? GROUP BY cycle ORDER BY cycle", (rid,)):
    print(f"ciclo {row['cycle']}: {row['n']} query")
    for t in row["txt"].split(" || "):
        print("   -", t[:95])

print("\n=== FONTI NUOVE PER CICLO ===")
rows = q("SELECT cycle, COUNT(*) n FROM events WHERE run_id=? AND "
         "type='source_fetched' GROUP BY cycle ORDER BY cycle", (rid,))
for row in rows:
    print(f"ciclo {row['cycle']}: {row['n']} fonti")

print("\n=== EVENTI GAP / TERMINAZIONE ===")
for row in q("SELECT cycle, type, substr(payload_json,1,180) p FROM events "
             "WHERE run_id=? AND type IN ('gap_detected','cycle_completed',"
             "'run_completed','run_failed') ORDER BY id", (rid,)):
    print(f"c{row['cycle']} {row['type']}: {row['p']}")

print("\n=== REPORT ===")
rep = conn.execute("SELECT title, substr(markdown,1,400) m FROM reports "
                   "WHERE run_id=?", (rid,)).fetchone()
if rep:
    print("TITOLO:", rep["title"])
    print(rep["m"])
else:
    print("NESSUN REPORT (run interrotto prima della scrittura)")
