# SrotaAI — Real-Time Pharmacovigilance for India

**AI for Bharat 2026 · Theme 6 — Real-Time Social Listening for Patient Safety Signals.**

SrotaAI continuously ingests adverse-event signals from openFDA, Reddit, RSS feeds, and patient-review sites; runs PRR / χ² / IC disproportionality math with MHRA thresholds; and surfaces tier-coded signals through a FastAPI dashboard. Every action is recorded in a tamper-evident hash-chained audit log.

---

## Quick start

**Live demo:** *(deployed link goes here)*

### Run locally

**Requirements:** Python 3.11 or newer, git.

> ⚠️ **Important — pick the right command for your OS.**
> `run.sh` is for Linux/macOS only. **Do not double-click it on Windows** — it will not work and may launch WSL.

#### Windows (PowerShell, cmd, or double-click)

```cmd
git clone https://github.com/KritikaTri/srota-ai.git
cd srota-ai
run.bat
```

Or just double-click `run.bat` in File Explorer. Then open **http://localhost:8001**.

#### macOS / Linux

```bash
git clone https://github.com/KritikaTri/srota-ai.git
cd srota-ai
chmod +x run.sh
./run.sh
```

Then open **http://localhost:8001**.

The script (whichever flavour) will:
1. Create a `.venv/` virtual environment
2. Install dependencies from `requirements.txt`
3. Initialise the SQLite database from the included seed snapshot
4. Start the server on port 8001

```bash
./run.sh stop      # or:  run.bat stop      stop the server
./run.sh restart   # or:  run.bat restart   restart with latest code
```

#### Docker (any OS — most reliable)

```bash
git clone https://github.com/KritikaTri/srota-ai.git
cd srota-ai
docker build -t srotaai .
docker run -p 8001:8000 srotaai
```

Then open **http://localhost:8001**.

#### Manual fallback (any OS, no scripts)

```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

pip install -r requirements.txt
copy srotaai_seed.db srotaai.db   # Windows
cp srotaai_seed.db srotaai.db     # Linux/macOS
python -m uvicorn srotaai.web.app:app --host 0.0.0.0 --port 8001
```

### Troubleshooting

| Problem | Fix |
|---|---|
| Double-clicking `run.sh` on Windows opens WSL or fails | Use `run.bat` instead. `.sh` files are Linux/macOS only. |
| `python: command not found` (Windows) | Install Python 3.11+ from [python.org](https://python.org), **check "Add Python to PATH"** during install, restart your terminal. |
| `python3: command not found` (Linux/macOS) | Install Python 3.11+ from [python.org](https://python.org) or your package manager (`brew install python@3.11`, `apt install python3.11`). |
| `port 8001 already in use` | Stop the existing server (`./run.sh stop` / `run.bat stop`), or pick a new port: `PORT=9000 ./run.sh` (Linux/macOS) / `set PORT=9000 && run.bat` (Windows). |
| `pip install` fails on `lxml` / `cryptography` | Linux: `sudo apt install build-essential python3-dev`. macOS: `xcode-select --install`. Windows: install [Visual C++ Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/). |
| Page is blank / CSS broken | Hard-refresh (Ctrl+Shift+R / Cmd+Shift+R). |
| Nothing works at all | Try the **Docker** path — it sidesteps every Python/PATH issue. |

**Health check:** `GET /_ping` returns a zero-CSS heartbeat page.

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
