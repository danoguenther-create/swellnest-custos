#!/usr/bin/env python3
"""Swellnest Custos — Textversion des Prototyps.

Nimmt kurze Ausgaben-Notizen per Telegram entgegen ("Casa Ribeira, esquentador,
Joao, 180"), macht daraus eine strukturierte Zeile und meldet Wiederholungen
("dritter Boiler in dieser Casa in 6 Wochen").

Bewusst klein: eine Datei, SQLite, kein Framework. Parsing laeuft ueber die
lokale Claude-Code-Installation; faellt sie aus, greift ein simpler Parser.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import shutil

import requests

ENV_PATH = Path(__file__).with_name(".env")
API = "https://api.telegram.org/bot{token}/{method}"
REPEAT_WINDOW_DAYS = 90
CLAUDE_TIMEOUT = 60
# systemd-User-Dienste haben ~/.local/bin nicht im PATH — deshalb absolut aufloesen.
CLAUDE_BIN = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
# Der Parser bekommt kein Werkzeug, keine MCP-Server, keine Settings/Hooks und laeuft in
# einem leeren Verzeichnis — er sieht nur den Nachrichtentext, nie die .env daneben.
CLAUDE_ARGS = ["-p", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
               "--setting-sources", ""]
SANDBOX = tempfile.mkdtemp(prefix="custos-parse-")


def log(level, msg):
    """Eine Zeile mit Zeitstempel. Bewusst ohne Nachrichteninhalt: keine Betraege im Log."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} [{level}] {msg}", file=sys.stderr if level in ("warn", "error") else sys.stdout,
          flush=True)


# ---------------------------------------------------------------- Konfiguration

def load_env(path=ENV_PATH):
    cfg = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        cfg[key.strip()] = value.strip()
    return cfg


CFG = load_env()
TOKEN = CFG["TELEGRAM_BOT_TOKEN"]
DB_PATH = CFG.get("SWELLNEST_DB", str(Path(__file__).with_name("custos.sqlite3")))
ALLOWED = {int(c) for c in CFG.get("TELEGRAM_ALLOWED_CHATS", "").split(",") if c.strip()}


# ---------------------------------------------------------------------- Storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT    NOT NULL,
    chat_id   INTEGER NOT NULL,
    casa      TEXT    NOT NULL,
    casa_key  TEXT    NOT NULL,
    servico   TEXT,
    servico_key TEXT,
    pessoa    TEXT,
    cents     INTEGER,
    raw       TEXT    NOT NULL,
    parser    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_casa ON entries(casa_key, ts);
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);
"""


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def get_offset(conn):
    row = conn.execute("SELECT v FROM state WHERE k='offset'").fetchone()
    return int(row["v"]) if row else 0


def set_offset(conn, value):
    conn.execute("INSERT INTO state(k,v) VALUES('offset',?) "
                 "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(value),))
    conn.commit()


def norm(text):
    """Vergleichsschluessel: ohne Akzente, klein, ohne Fuellwoerter."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"\b(a|o|as|os|da|de|do|das|dos|casa|the)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


# ----------------------------------------------------------------------- Parsing

PARSE_PROMPT = """Extrahiere aus einer kurzen Ausgaben-Notiz eines Immobilienbetreibers in Portugal strukturierte Daten.
Die Notiz ist meist Portugiesisch, manchmal Englisch oder Deutsch, oft unvollstaendig und umgangssprachlich.

Felder:
- casa: das Haus/Objekt (z.B. "Casa Ribeira", "Swellnest")
- servico: was gemacht wurde (z.B. "esquentador", "estores", "canalizacao")
- pessoa: Handwerker/Dienstleister, falls genannt
- valor: Betrag in Euro als Zahl. Auch ausgeschriebene Zahlen ("cento e oitenta" = 180)
  und Naeherungen ("mais ou menos 180" = 180) aufloesen.

Regeln:
- Antworte AUSSCHLIESSLICH mit einem JSON-Objekt, ohne Markdown, ohne Erklaerung.
- Nicht raten: unklare Felder auf null setzen. Ein falscher Betrag ist schlimmer als kein Betrag.
- Schluessel immer alle vier: casa, servico, pessoa, valor.

Notiz: {text}"""


def parse_with_claude(text):
    # Prompt als letztes Argument: --tools nimmt eine Liste und wuerde es sonst schlucken.
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, *CLAUDE_ARGS, PARSE_PROMPT.format(text=text)],
            capture_output=True, text=True, timeout=CLAUDE_TIMEOUT, cwd=SANDBOX,
        )
    except subprocess.TimeoutExpired:
        log("warn", f"claude: Timeout nach {CLAUDE_TIMEOUT}s — Fallback-Parser")
        return None
    except FileNotFoundError:
        log("warn", f"claude nicht gefunden unter {CLAUDE_BIN} — Fallback-Parser")
        return None
    if proc.returncode != 0:
        log("warn", f"claude: Exit {proc.returncode} — Fallback-Parser")
        return None
    match = re.search(r"\{.*\}", proc.stdout, re.S)
    try:
        data = json.loads(match.group(0)) if match else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        log("warn", "claude: kein JSON in der Antwort — Fallback-Parser")
        return None
    return data


def parse_fallback(text):
    """Ohne Claude: Komma-Felder plus erste Zahl. Bewusst konservativ."""
    money = re.search(r"(\d+(?:[.,]\d{1,2})?)\s*(?:eur|euro|euros|€)?", text, re.I)
    parts = [p.strip() for p in re.split(r"[,;]", text) if p.strip()]
    casa = parts[0] if parts else None
    servico = parts[1] if len(parts) > 1 else None
    pessoa = parts[2] if len(parts) > 2 and not re.fullmatch(
        r"[^a-zA-Z]*", parts[2]) and not re.search(r"\d", parts[2]) else None
    return {
        "casa": casa,
        "servico": servico,
        "pessoa": pessoa,
        "valor": float(money.group(1).replace(",", ".")) if money else None,
    }


def parse_entry(text):
    data = parse_with_claude(text)
    parser = "claude"
    if not data or not data.get("casa"):
        data = parse_fallback(text)
        parser = "fallback"
    return data, parser


# -------------------------------------------------------------------- Auswertung

def plural(n, singular, plural_form):
    return f"{n} {singular if n == 1 else plural_form}"


def money(cents):
    return "—" if cents is None else f"{cents/100:,.0f} €".replace(",", ".")


def month_bounds():
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()


def find_repeats(conn, casa_key, servico_key):
    if not servico_key:
        return None
    since = (datetime.now(timezone.utc) - timedelta(days=REPEAT_WINDOW_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT ts, cents FROM entries WHERE casa_key=? AND servico_key=? AND ts>=? ORDER BY ts",
        (casa_key, servico_key, since)).fetchall()
    if len(rows) < 2:
        return None
    total = sum(r["cents"] or 0 for r in rows)
    first = datetime.fromisoformat(rows[0]["ts"])
    weeks = max(1, round((datetime.now(timezone.utc) - first).days / 7))
    return len(rows), total, weeks


def render_entry(conn, row_id):
    row = conn.execute("SELECT * FROM entries WHERE id=?", (row_id,)).fetchone()
    bits = [row["casa"]]
    if row["servico"]:
        bits.append(row["servico"])
    if row["pessoa"]:
        bits.append(row["pessoa"])
    lines = [f"✓ {' · '.join(bits)} · {money(row['cents'])}"]

    month_row = conn.execute(
        "SELECT COUNT(*) n, SUM(cents) s FROM entries WHERE casa_key=? AND ts>=?",
        (row["casa_key"], month_bounds())).fetchone()
    lines.append(f"   Este mês nesta casa: {money(month_row['s'])}"
                 f" ({plural(month_row['n'], 'registo', 'registos')})")

    if row["cents"] is None:
        lines.append("   ⚠️ Sem valor — responde com o montante ou usa /apagar")

    repeat = find_repeats(conn, row["casa_key"], row["servico_key"])
    if repeat:
        n, total, weeks = repeat
        lines.append(f"   🔁 {n}× «{row['servico']}» nesta casa em "
                     f"{plural(weeks, 'semana', 'semanas')} — total {money(total)}")

    if row["parser"] == "fallback":
        lines.append("   (interpretação simples — confirma os campos)")
    return "\n".join(lines)


# ---------------------------------------------------------------------- Telegram

def call(method, **params):
    try:
        resp = requests.post(API.format(token=TOKEN, method=method), data=params, timeout=70)
        return resp.json()
    except requests.RequestException as exc:
        log("warn", f"{method}: {exc}")
        return {"ok": False}


def send(chat_id, text):
    call("sendMessage", chat_id=chat_id, text=text)


HELP = """Swellnest Custos — registo de gastos

Escreve uma nota curta depois de cada trabalho:
  Casa Ribeira, esquentador, João, 180

Comandos:
  /casas — gastos por casa este mês
  /mes — total do mês
  /ultimos — últimos registos
  /apagar — apaga o último registo"""


def cmd_casas(conn, chat_id):
    rows = conn.execute(
        "SELECT casa, COUNT(*) n, SUM(cents) s FROM entries WHERE ts>=? "
        "GROUP BY casa_key ORDER BY s DESC NULLS LAST", (month_bounds(),)).fetchall()
    if not rows:
        return send(chat_id, "Ainda sem registos este mês.")
    lines = ["📊 Gastos por casa — este mês", ""]
    for r in rows:
        lines.append(f"{r['casa']}: {money(r['s'])} ({r['n']})")  # Anzahl kompakt
    total = sum(r["s"] or 0 for r in rows)
    lines += ["", f"Total: {money(total)}"]
    send(chat_id, "\n".join(lines))


def cmd_mes(conn, chat_id):
    row = conn.execute("SELECT COUNT(*) n, SUM(cents) s FROM entries WHERE ts>=?",
                       (month_bounds(),)).fetchone()
    send(chat_id, f"Este mês: {money(row['s'])} em {plural(row['n'], 'registo', 'registos')}.")


def cmd_ultimos(conn, chat_id):
    rows = conn.execute("SELECT * FROM entries ORDER BY id DESC LIMIT 10").fetchall()
    if not rows:
        return send(chat_id, "Ainda sem registos.")
    lines = ["🧾 Últimos registos", ""]
    for r in rows:
        day = datetime.fromisoformat(r["ts"]).strftime("%d/%m")
        bits = " · ".join(b for b in (r["casa"], r["servico"], r["pessoa"]) if b)
        lines.append(f"{day}  {bits} — {money(r['cents'])}")
    send(chat_id, "\n".join(lines))


def cmd_apagar(conn, chat_id):
    row = conn.execute("SELECT * FROM entries WHERE chat_id=? ORDER BY id DESC LIMIT 1",
                       (chat_id,)).fetchone()
    if not row:
        return send(chat_id, "Nada para apagar.")
    conn.execute("DELETE FROM entries WHERE id=?", (row["id"],))
    conn.commit()
    send(chat_id, f"🗑 Apagado: {row['casa']} · {money(row['cents'])}")


def handle(conn, message):
    chat_id = message["chat"]["id"]
    if ALLOWED and chat_id not in ALLOWED:
        log("info", f"ignoriert: chat {chat_id}")
        return
    if any(k in message for k in ("voice", "audio", "video_note")):
        return send(chat_id, "🎙 Por agora só percebo texto. Escreve assim:\n"
                             "Casa Ribeira, esquentador, João, 180")
    text = (message.get("text") or "").strip()
    if not text:
        return

    low = text.lower()
    if low.startswith(("/start", "/help", "/ajuda")):
        return send(chat_id, HELP)
    if low.startswith("/casas"):
        return cmd_casas(conn, chat_id)
    if low.startswith("/mes"):
        return cmd_mes(conn, chat_id)
    if low.startswith("/ultimos"):
        return cmd_ultimos(conn, chat_id)
    if low.startswith("/apagar"):
        return cmd_apagar(conn, chat_id)
    if low.startswith("/"):
        return send(chat_id, HELP)

    data, parser = parse_entry(text)
    casa = (data.get("casa") or "").strip()
    if not casa:
        return send(chat_id, "Não percebi a casa. Exemplo:\nCasa Ribeira, esquentador, João, 180")

    valor = data.get("valor")
    try:
        cents = int(round(float(valor) * 100)) if valor is not None else None
    except (TypeError, ValueError):
        cents = None

    servico = (data.get("servico") or "").strip() or None
    if cents is None and servico is None:
        return send(chat_id, "Não registei nada — falta o trabalho ou o valor. Exemplo:\n"
                             "Casa Ribeira, esquentador, João, 180")

    cur = conn.execute(
        "INSERT INTO entries (ts, chat_id, casa, casa_key, servico, servico_key, pessoa, cents, raw, parser)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), chat_id, casa, norm(casa), servico,
         norm(servico), (data.get("pessoa") or "").strip() or None, cents, text, parser))
    conn.commit()
    log("entry", f"id={cur.lastrowid} chat={chat_id} parser={parser}")
    send(chat_id, render_entry(conn, cur.lastrowid))


def main():
    conn = db()
    log("start", f"Swellnest Custos · db={DB_PATH} · allowlist={sorted(ALLOWED) or 'offen'}"
                 f" · parser={CLAUDE_BIN}")
    while True:
        result = call("getUpdates", offset=get_offset(conn), timeout=50,
                      allowed_updates=json.dumps(["message"]))
        if not result.get("ok"):
            time.sleep(5)
            continue
        for update in result.get("result", []):
            set_offset(conn, update["update_id"] + 1)
            message = update.get("message")
            if message:
                try:
                    handle(conn, message)
                except Exception as exc:  # ein kaputter Eintrag darf den Bot nicht stoppen
                    log("error", repr(exc))


if __name__ == "__main__":
    main()
