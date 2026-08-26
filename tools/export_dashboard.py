#!/usr/bin/env python3
"""Export the bot's SQLite ledger into docs/data.json for the dashboard.

Usage:  python3 tools/export_dashboard.py [path/to/custos.sqlite3]
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / "swellnest-bot/custos.sqlite3")
OUT = Path(__file__).resolve().parent.parent / "docs" / "data.json"

conn = sqlite3.connect(DB)
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
OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
print(f"{len(entries)} entries -> {OUT}")
