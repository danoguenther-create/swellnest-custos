#!/usr/bin/env python3
"""Export the bot's SQLite ledger into a data.json the dashboard can read.

Real data must never end up in this repository: docs/ is published on
GitHub Pages. The export therefore writes outside the repo by default and
refuses any output path inside it.

Usage:
  python3 tools/export_dashboard.py [--db PATH] [--out PATH]

Defaults: --db ~/swellnest-bot/custos.sqlite3
          --out ~/swellnest-bot/export/data.json
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--db", type=Path, default=Path.home() / "swellnest-bot/custos.sqlite3")
parser.add_argument("--out", type=Path, default=Path.home() / "swellnest-bot/export/data.json")
args = parser.parse_args()

out = args.out.expanduser().resolve()
if out == REPO or REPO in out.parents:
    sys.exit(f"Refusing to write real data inside the repository ({REPO}).\n"
             f"docs/ is public on GitHub Pages. Choose an --out path outside it.")

conn = sqlite3.connect(args.db.expanduser())
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT * FROM entries ORDER BY ts").fetchall()

entries = [{
    "ts": r["ts"][:19],
    "house": r["casa"],
    "service": r["servico_key"] or "",
    "service_label": (r["servico"] or "—").capitalize(),
    "person": r["pessoa"],
    "amount": None if r["cents"] is None else r["cents"] / 100,
} for r in rows]

payload = {
    "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    "demo": False,
    "currency": "EUR",
    "houses": sorted({e["house"] for e in entries}),
    "entries": entries,
}
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
print(f"{len(entries)} entries -> {out}")
