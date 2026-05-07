# SrotaAI — Real-Time Pharmacovigilance for India

**AI for Bharat 2026 · Theme 6 — Real-Time Social Listening for Patient Safety Signals.**

SrotaAI continuously ingests adverse-event signals from openFDA, Reddit, RSS feeds, and patient-review sites; runs PRR / χ² / IC disproportionality math with MHRA thresholds; and surfaces tier-coded signals through a FastAPI dashboard. Every action is recorded in a tamper-evident hash-chained audit log.

---

## Quick start

```bash
./run.sh           # start in background
./run.sh restart   # restart with latest code
./run.sh stop      # stop the server
```

The script activates `.venv/`, starts uvicorn as a daemon (PID in `.server.pid`, logs in `server.log`), and prints the URL — typically `http://<host>:8001`.

**Health check:** `GET /_ping` returns a zero-CSS heartbeat page.

**Requirements:** Python 3.11+ and `pip install -r requirements.txt`.

---

## What's where

| Path | Purpose |
|---|---|
| `srotaai/web/app.py`            | FastAPI app — Home, Projects, Signals, Compliance, Agentic Onboarding, Search, About |
| `srotaai/web/templates/`        | Jinja templates (Tailwind-styled) |
| `srotaai/runner.py`             | `python -m srotaai.runner projects/<f>.yaml` — fans a project to its connectors, advances watermark, deduplicates, writes through the store |
| `srotaai/store.py`              | SQLite store. Tables: `projects`, `sources`, `records`, `watermarks`, `signals`, `audit_log`. Uses `BEGIN IMMEDIATE` so concurrent audit appends stay chained |
| `srotaai/signals.py`            | `python -m srotaai.signals <project_id>` — PRR / χ² / IC with Haldane–Anscombe correction; writes into `signals` |
| `srotaai/audit.py`              | `python -m srotaai.audit <db>` — verifies the Merkle-chained audit log |
| `srotaai/connectors_extra.py`   | `html_stealth`, `openfda`, real Reddit, X-stub. Auto-registers into the connector REGISTRY |
| `srotaai/ner.py` / `pii.py`     | India-locale NER (Crocin → paracetamol …) and PII redaction (phone, Aadhaar, PAN, …) |
| `spikes/`                       | Round-1 spike scripts. `spike1_connectors.py` is **imported at runtime** as the connector registry — do not delete |
| `projects/pv-india-otc.yaml`    | Live demo project: openFDA, Reddit, FDA Recalls RSS, 1mg reviews |

---

## End-to-end demo (CLI)

```bash
# 1. Ingest into a fresh DB (idempotent — second run inserts 0)
python -m srotaai.runner projects/pv-india-otc.yaml --db srotaai_smoke.db
python -m srotaai.runner projects/pv-india-otc.yaml --db srotaai_smoke.db   # 0 new

# 2. Compute signals
python -m srotaai.signals pv-india-otc --db srotaai_smoke.db --min-n 2

# 3. Verify audit chain
python -m srotaai.audit srotaai_smoke.db --tail 10

# 4. PII validation set (Faker)
python -m srotaai.pii eval | python -c "import sys,json; print(json.loads(sys.stdin.read())['recall_by_type'])"
```

---

## Configuring a new data source (no code required)

SrotaAI ships with these connectors out of the box: `openfda`, `reddit`, `rss`, `html_stealth` (works with **any URL** via CSS selectors), and `whatsapp` (Meta Cloud webhook, fixture-driven).

To onboard a new source, add a block to your project YAML — no Python edits, no redeploy. For example, to start tracking 1mg paracetamol reviews:

```yaml
sources:
  - name: 1mg-paracetamol-reviews
    connector: html_stealth
    enabled: true
    params:
      url: https://www.1mg.com/drugs/paracetamol-501-reviews
      review_selector: ".ReviewItem__review"
    latency: daily
```

Restart the server and records flow through the same ingestion → NER → PRR → audit pipeline.

---

## Notes for judges

- All adverse-event data is **real** (openFDA + Reddit). No synthetic rows in the live DB.
- Audit chain serializes via `BEGIN IMMEDIATE` so concurrent writers can't fork the chain.
- HTML responses send `Cache-Control: no-store`; CSS is cache-busted with a build-id; use Chrome / Edge incognito for the cleanest demo (VS Code Simple Browser caches aggressively).
