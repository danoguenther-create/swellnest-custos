# Swellnest Custos

A five-second way to record what maintenance actually costs, per house.

Antonio runs a small portfolio of houses near Ericeira, Portugal. Roughly 90% of his daily
problems are maintenance: something breaks, he phones the right tradesperson, it gets fixed.
The phone call is the fastest tool he has — a good agent does not try to replace it.

The problem is that **nothing survives the call.** No record of which house, which problem,
who was called, or what it cost. So the same water heater in the same house can eat several
hundred euros across five separate call-outs, and that only becomes visible months later when
the accountant totals up a supplier.

This bot closes that gap with the smallest possible habit: after hanging up, send one short
message.

```
Casa Ribeira, esquentador, João, 180
```

Three seconds later:

```
✓ Casa Ribeira · Esquentador · João · 180 €
   Este mês nesta casa: 540 € (3 registos)
   🔁 3× «esquentador» nesta casa em 6 semanas — total 540 €
```

That last line is the point. It is not a time saving — it is information that did not exist
before, delivered early enough to act on.

## Dashboard

**[View the dashboard →](https://danoguenther-create.github.io/swellnest-custos/)**

Chat is the day-to-day surface; the dashboard is for the moments chat is bad at — comparing
houses over time, or showing an owner where their money went. It is published with **synthetic
demo data**; no real figures are in this repository.

## How it works

| Piece | Notes |
|---|---|
| Intake | Telegram long polling, restricted to an allowlist of chat IDs |
| Parsing | The local `claude` CLI turns loose Portuguese into structured fields — it resolves `cento e oitenta` to `180` and strips `outra vez` from the job name, which a naive parser would keep (and then miss the repeat) |
| Fallback | If the CLI is unavailable, a deterministic comma parser takes over and the reply says so |
| Storage | SQLite, one table |
| Reporting | `/casas`, `/mes`, `/ultimos`, `/apagar` |

**No amount is ever guessed.** If the value is unclear the entry is stored without one and
flagged. A ledger that is 80% complete but silently wrong is worse than one with visible gaps —
you end up checking both.

## Layout

```
bot/custos.py                 the bot (single file, stdlib + requests)
bot/.env.example              configuration template
bot/swellnest-custos.service  systemd user unit
tools/export_dashboard.py     SQLite -> docs/data.json
docs/                         the dashboard (GitHub Pages)
```

## Running it

```bash
cp bot/.env.example bot/.env    # add bot token + allowed chat IDs
chmod 600 bot/.env
python3 bot/custos.py
```

Parsing uses whatever `claude` login exists on the machine, so no API key is required. Without
the CLI the bot still runs on the fallback parser.

To export real data for the dashboard:

```bash
python3 tools/export_dashboard.py            # -> ~/swellnest-bot/export/data.json
```

The export deliberately writes **outside** this repository and refuses any path inside it:
`docs/` is public on GitHub Pages, so one careless push would publish real figures. Serve the
exported file privately (for example next to a copy of `docs/index.html` on the VPS).

The parser call is isolated: no tools, no MCP servers, no settings or hooks, no session
persistence, and an empty working directory. It only ever sees the message text.

## Status

Prototype, built to demonstrate one idea before committing to the larger plan. Voice notes are
the intended input — transcription is deliberately not wired up yet, and when it is it should
run locally, so that someone else's business data never leaves the machine.
