# SrotaAI — Live Demo Flow & Speaker Script

> **Total runtime: 5–7 min.** Tight, confident, every click has a line.
> **Browser:** Incognito Chrome / Edge at `http://localhost:8001` (Simple Browser caches CSS — don't use it).
> **Pre-flight (do this 30 min before):** `./run.sh restart` · open incognito · pre-load `/projects` and `/signals` in two tabs · have a terminal open in the repo root with the venv activated.

---

## Pre-flight checklist (run once before judges arrive)

Run these in a terminal with the venv active. Output is the proof — keep the terminal visible.

```bash
# 1. Server is healthy
curl -s http://localhost:8001/healthz
# expect: {"ok": true, ...}

# 2. Live demo project has data
python -m srotaai.runner projects/pv-india-otc.yaml --db srotaai.db
# expect: rows ingested first run; "0 new" on a re-run (proves idempotency)

# 3. Signals are computed
python -m srotaai.signals pv-india-otc --db srotaai.db --min-n 2
# expect: a list of signals with PRR / chi2 / IC values

# 4. Audit chain is intact
python -m srotaai.audit srotaai.db --tail 5
# expect: chain-verify OK
```

If any of those fail, fix before demoing. **Never demo a broken pipeline live.**

---

## The 5-Minute Demo Flow (recommended path)

| # | Time | Where you are | What you click | What you say |
|---|---|---|---|---|
| 1 | 0:00 | Title slide | — | Hook |
| 2 | 0:30 | `/` Home | scroll | Frame the engine |
| 3 | 1:00 | `/projects` → `pv-india-otc` | open detail | Show real data |
| 4 | 2:00 | `/signals` | click a Strong-tier chip | Show the math |
| 5 | 3:00 | `/signals/{id}` | scroll to records | Show traceability |
| 6 | 3:45 | `/projects/new` | **create live** | Show extensibility |
| 7 | 5:00 | Terminal | `srotaai.audit ... --tail` | Close on auditability |

Detailed walkthrough below.

---

## Step 1 — Hook (0:00 – 0:30)

**On screen:** Title slide.

**Say:**
> "Srota means *source* in Sanskrit. India's drug-safety signals arrive 6 to 18 months after patients first report them online. We built the engine that closes that gap. Everything I show you in the next 5 minutes is running on real openFDA and Reddit data, and reproducible from our README."

---

## Step 2 — Frame the product (0:30 – 1:00)

**On screen:** `http://localhost:8001/` (Home / Overview).

**Do:** Scroll once, slowly. Point at the headline metrics (active projects, total records, signals by tier).

**Say:**
> "This is SrotaAI's overview. Five live connectors, real records ingested, signals tier-coded by MHRA thresholds. No mocked rows — what you see is what we crawled."

---

## Step 3 — Open the live demo project (1:00 – 2:00)

**On screen:** `/projects` → click **India OTC Pharmacovigilance** (`pv-india-otc`).

**Do:** Point at the sources list (openFDA, Reddit `r/india`+`r/AskDocs`, FDA Recalls RSS, 1mg).

**Say:**
> "A *project* is the unit of work. This one monitors common Indian OTC drugs — Crocin, Dolo, Telma, Ecosprin — across four real sources. Each source declares its own latency: real-time for Reddit, daily for openFDA and RSS. The whole project is one YAML file in our repo — no code."

**Optional:** click **Run now** to trigger an ingest. It returns instantly because of watermarks.

> "Notice — second run, zero new rows. The watermark is honest. That same watermark is what lets us compute defensible trend deltas across windows."

---

## Step 4 — Show a tier-coded signal (2:00 – 3:00)

**On screen:** `/signals`. Sort by tier (Strong first).

**Do:** Hover the tier-coded chips. Click a Strong or Moderate signal.

**Say:**
> "Signals are scored using PRR, chi-squared with Yates correction, and Bayesian IC — the same disproportionality math the WHO Uppsala Monitoring Centre uses, with a Haldane–Anscombe correction for sparse cells. We apply MHRA's regulator thresholds: PRR ≥ 2, chi-squared ≥ 4, n ≥ 3. This is not 'AI flagged it' — this is regulator-grade arithmetic."

---

## Step 5 — Show traceability (3:00 – 3:45)

**On screen:** `/signals/{sig_id}` — the signal detail page.

**Do:** Scroll to the contributing records list. Point at a Reddit URL. Point at the audit hash if visible.

**Say:**
> "Click any signal and you see the raw records that produced it, the math behind the score, and the audit hash. Every row links back to its source URL — Reddit post, openFDA report, RSS item. Auditors don't have to trust us; they can verify."

---

## Step 6 — Create a new project LIVE (3:45 – 5:00)

This is the wow moment. Have these inputs **memorized** so you can type without thinking.

**On screen:** `/projects/new`.

### Exact inputs to type

| Field | Exact value |
|---|---|
| **Project ID** | `vaccine-watch-demo` |
| **Display name** | `Vaccine Safety Watch — Live Demo` |
| **Description** | `Real-time monitoring of COVID-19 and seasonal vaccine adverse-event chatter across Reddit and openFDA.` |
| **Keywords** | `covaxin, covishield, mRNA vaccine, myocarditis, anaphylaxis, fever, fatigue` |
| **Lookback window (days)** | `30` |
| **Fetch cadence** | `Daily · every 24 h` |

### Sources to add (click "+ Add source" three times)

**Source 1 — Reddit:**
- Name: `reddit-askdocs`
- Connector: `Reddit (subreddit)`
- Subreddit name: `AskDocs`
- Enabled: ✓

**Source 2 — openFDA:**
- Name: `openfda-vaccines`
- Connector: `openFDA (FAERS)`
- Drug / search term: `COVID-19 VACCINE`
- Enabled: ✓

**Source 3 — FDA Recalls RSS:**
- Name: `fda-vaccine-recalls`
- Connector: `RSS / Atom feed`
- Feed URL: `https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/drugs/rss.xml`
- Enabled: ✓

**Click:** *Create & Start Fetching*.

**Say while typing:**
> "Watch how fast this is. I name the project, drop in the keywords I care about — Covaxin, Covishield, myocarditis, anaphylaxis. I add three sources from a dropdown — a subreddit, an openFDA query, an FDA recalls feed. No code. No redeploy. No YAML editing. The connector registry takes care of the rest."

**After clicking Create:**
> "And it's live. The scheduler picks it up on the daily cadence, ingests run, NER tags the entities, PII gets redacted, signals get scored — same pipeline as the project we were just looking at."

> *(If a fourth source slot is available and you have time: add a* `Webpage` *source — URL* `https://www.1mg.com/drugs/paracetamol-501-reviews` *— and say: "And here's the killer: any webpage with reviews. The stealth fetcher handles it. This is what onboarding a new pharma review site looks like in production.")*

---

## Step 7 — Close on the audit chain (5:00 – 5:30)

**On screen:** Switch to terminal.

**Run:**
```bash
python -m srotaai.audit srotaai.db --tail 10
```

**Say:**
> "Last thing. Every action you just watched — every project create, every ingest, every signal — is appended to a Merkle-style hash chain. One CLI verifies the whole thing. Tamper-evident by construction. This is the line between a hackathon prototype and something a regulator would actually deploy."

---

## Optional power moves (use if a judge probes)

### "Show me how you'd add an entirely new website."
Open [projects/pv-india-otc.yaml](projects/pv-india-otc.yaml) in the editor.

**Say:**
> "Here's the YAML. Adding a new source is a 6-line block — `name`, `connector: html_stealth`, `params: { url, list_selector, title_selector, body_selector }`, `latency`, `enabled`. Restart, and it's in the pipeline. That's the *Extensibility* criterion in the rubric — we built for it from day one."

### "How do you measure your PII redaction?"
**Run:**
```bash
python -m srotaai.pii eval | python -c "import sys,json; print(json.loads(sys.stdin.read())['recall_by_type'])"
```

**Say:**
> "Faker generates synthetic records with planted phone numbers, Aadhaar, PAN, emails. We measure per-type recall. Not vibes — numbers."

### "What about Indian languages / brand names?"
Open [srotaai/synonyms.py](srotaai/synonyms.py).

**Say:**
> "English NER alone misses Indian colloquialisms. We ship a brand→generic map — Crocin maps to paracetamol, Dolo to paracetamol, Telma to telmisartan. Multilingual NER for Hindi, Bengali, and Tamil is the Q3 roadmap item."

### "What's your agentic story?"
**Say:**
> "Two layers. First, the connector YAML itself is structured — an LLM can draft it from a URL plus an intent string, which we wired in our spike directory. Second, on the roadmap: auto-discovery of new sources matching a project's keywords, and LLM-drafted MedDRA coding suggestions with human-in-the-loop sign-off. The infrastructure is ready; we sequenced the UI surface for the next sprint."

### "Trends say 'preliminary' on first run — why?"
**Say:**
> "By design. A trend is a delta between two independently-watermarked windows. Faking a delta from a single ingest would be exactly the kind of unauditable shortcut a regulator would reject. We made the auditable choice."

---

## Failure recovery (if something breaks live)

| Failure | Recovery line |
|---|---|
| Page won't load | "Cache issue — give me one second." Hard-refresh (Ctrl+Shift+R). If still broken, switch to the terminal demo (steps 7 + power moves). |
| Ingest is slow | "Rate-limited by openFDA — the project is created, the run is queued; let me show you an existing project that's already populated." Switch back to `/projects/pv-india-otc`. |
| Form rejects input | "Looks like I fat-fingered the project ID — must be lowercase-dashes. Re-enter and continue." |
| Server down | "Let me restart — `./run.sh restart` — meanwhile let me walk you through the architecture diagram." Buy 20 seconds with the deck. |

**Golden rule:** never apologize twice. State the fix, recover, move on.

---

## What the judges should remember

1. **Real data, real math, real audit.** Three things to repeat in closing.
2. **One YAML to onboard a source.** The extensibility proof.
3. **The audit chain.** The thing nobody else built.

End every demo with: *"Everything you just saw is in the public repo. Clone it, run `./run.sh`, and you'll have the same screen in five minutes."*
