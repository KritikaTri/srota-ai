"""
Signal job — read `records` from the store, run NER + sentiment, build
(drug, event) co-occurrence pairs, compute disproportionality (PRR/χ²/IC,
matching spike3's MHRA thresholds), and write rows into `signals`.

Run:
    .venv/bin/python -m srotaai.signals pv-india-otc
or
    .venv/bin/python -m srotaai.signals pv-india-otc --db srotaai.db
"""
from __future__ import annotations

import argparse
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .store import Store, DEFAULT_DB_PATH
from . import ner
from . import sentiment as _sent


THRESH_PRR = 2.0
THRESH_CHI2 = 4.0
THRESH_N = 3


def _compute(rows: list[tuple[str, str]], min_n: int = THRESH_N,
             prr_thresh: float = THRESH_PRR, chi2_thresh: float = THRESH_CHI2):
    """Identical math to spike3.compute_signals — kept here so the module is self-contained."""
    drug_event = Counter(rows)
    drug_total = Counter(d for d, _ in rows)
    event_total = Counter(e for _, e in rows)
    N = len(rows)

    out = []
    for (drug, event), n_de in drug_event.items():
        if n_de < min_n:
            continue
        a = n_de
        b = drug_total[drug] - a
        c = event_total[event] - a
        d = max(0, N - a - b - c)

        a_, b_, c_, d_ = a, b, c, d
        if 0 in (a_, b_, c_, d_):                       # Haldane-Anscombe correction
            a_, b_, c_, d_ = a_ + 0.5, b_ + 0.5, c_ + 0.5, d_ + 0.5
        prr_num = a_ / (a_ + b_) if (a_ + b_) else 0
        prr_den = c_ / (c_ + d_) if (c_ + d_) else 1e-9
        prr = prr_num / prr_den if prr_den > 0 else 0
        expected_a = (a + b) * (a + c) / N if N else 1
        chi2 = ((abs(a - expected_a) - 0.5) ** 2) / expected_a if expected_a else 0
        ic = math.log2((a + 0.5) / (expected_a + 0.5)) if expected_a + 0.5 > 0 else 0

        is_signal = prr >= prr_thresh and chi2 >= chi2_thresh and a >= min_n
        out.append({
            "drug": drug, "event": event, "n": a,
            "prr": round(prr, 3), "chi2": round(chi2, 3), "ic": round(ic, 3),
            "is_signal": is_signal,
        })
    out.sort(key=lambda s: (-s["is_signal"], -s["prr"]))
    return out


def run(project_id: str, db_path: Path | None = None,
        sentiment_filter: bool = True,
        min_n: int = THRESH_N,
        prr_thresh: float = THRESH_PRR,
        chi2_thresh: float = THRESH_CHI2) -> dict:
    store = Store(db_path or DEFAULT_DB_PATH)
    proj = store.conn.execute(
        "SELECT id, name FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if not proj:
        store.close()
        raise SystemExit(f"unknown project '{project_id}'")

    rows = store.conn.execute(
        """SELECT r.text_redacted, r.posted_at
           FROM records r JOIN sources s ON s.id = r.source_id
           WHERE s.project_id = ?""",
        (project_id,),
    ).fetchall()

    pairs: list[tuple[str, str]] = []
    pair_examples: dict[tuple[str, str], list[str]] = {}
    n_records = 0
    n_with_pairs = 0
    earliest = None
    latest = None

    for row in rows:
        n_records += 1
        text = row["text_redacted"] or ""
        ts = row["posted_at"]
        if ts:
            if earliest is None or ts < earliest:
                earliest = ts
            if latest is None or ts > latest:
                latest = ts

        ner_res = ner.extract(text)
        if not ner_res.drug_event_pairs:
            continue
        if sentiment_filter:
            s = _sent.analyze(text)
            if s.label not in ("adverse", "negative"):
                continue
        n_with_pairs += 1
        # IMPORTANT: dedupe pairs WITHIN this record. The PV interpretation
        # of n is "distinct reports/records mentioning this drug × event",
        # not "raw co-occurrence count". A single Reddit post that says
        # "metformin gave me lactic acidosis. metformin is awful, lactic
        # acidosis ruined me" should count as 1, not 4. Keeps the math
        # honest and aligned with FAERS practice.
        for pair in set(ner_res.drug_event_pairs):
            pairs.append(pair)
            pair_examples.setdefault(pair, []).append(text[:140])

    signals = _compute(pairs, min_n=min_n, prr_thresh=prr_thresh,
                       chi2_thresh=chi2_thresh)
    flagged = [s for s in signals if s["is_signal"]]

    computed_at = datetime.now(timezone.utc).isoformat()
    with store._tx() as c:
        for s in signals:
            c.execute(
                """INSERT OR IGNORE INTO signals
                   (project_id, drug, event, prr, chi2, ic, n,
                    window_since, window_until, computed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (project_id, s["drug"], s["event"], s["prr"], s["chi2"],
                 s["ic"], s["n"], earliest, latest, computed_at),
            )
    store.append_audit("signal_job", "signals.computed", target=project_id,
                       payload={"records": n_records, "with_pairs": n_with_pairs,
                                "rows": len(signals), "flagged": len(flagged)})
    store.close()

    summary = {
        "project_id": project_id,
        "records_scanned": n_records,
        "records_with_pairs": n_with_pairs,
        "pair_count": len(pairs),
        "cells_evaluated": len(signals),
        "flagged_signals": flagged,
        "thresholds": {"prr": prr_thresh, "chi2": chi2_thresh, "n": min_n},
        "window_since": earliest,
        "window_until": latest,
        "computed_at": computed_at,
        "examples": {f"{k[0]}↔{k[1]}": v[:3] for k, v in pair_examples.items()},
    }
    return summary


def _print(summary: dict):
    print(f"\n=== Signal job: {summary['project_id']} ===")
    print(f"  records scanned    : {summary['records_scanned']}")
    print(f"  records with pairs : {summary['records_with_pairs']}")
    print(f"  pair instances     : {summary['pair_count']}")
    print(f"  cells evaluated    : {summary['cells_evaluated']}")
    print(f"  flagged signals    : {len(summary['flagged_signals'])}")
    for s in summary["flagged_signals"]:
        print(f"    🚨  {s['drug']:>22} ↔ {s['event']:<22}  "
              f"n={s['n']:<3} PRR={s['prr']:<5} χ²={s['chi2']:<6} IC={s['ic']}")
    print(f"  thresholds         : PRR≥{summary['thresholds']['prr']}, "
          f"χ²≥{summary['thresholds']['chi2']}, n≥{summary['thresholds']['n']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project_id")
    ap.add_argument("--db", default=None)
    ap.add_argument("--no-sentiment", action="store_true",
                    help="Skip sentiment filter (count all pairs)")
    ap.add_argument("--min-n", type=int, default=THRESH_N,
                    help=f"min co-occurrence count (MHRA default {THRESH_N})")
    ap.add_argument("--prr", type=float, default=THRESH_PRR)
    ap.add_argument("--chi2", type=float, default=THRESH_CHI2)
    args = ap.parse_args()
    db = Path(args.db) if args.db else None
    summary = run(args.project_id, db_path=db,
                  sentiment_filter=not args.no_sentiment,
                  min_n=args.min_n, prr_thresh=args.prr, chi2_thresh=args.chi2)
    _print(summary)


if __name__ == "__main__":
    main()
