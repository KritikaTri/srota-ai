"""Apply the installed ADE classifier to existing records that lack scores."""
from __future__ import annotations

import argparse
from pathlib import Path

from srotaai.ml.ade_classifier import load_bundle, predict_text
from srotaai.store import DEFAULT_DB_PATH, Store


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--force", action="store_true", help="rescore rows already carrying a model label")
    args = ap.parse_args()
    bundle = load_bundle()
    if bundle is None:
        raise SystemExit("No trained ADE model found; train one first (see srotaai/ml/README.md)")

    store = Store(args.db)
    where = "" if args.force else "WHERE ml_model_version IS NULL"
    rows = store.conn.execute(
        f"SELECT id, text_redacted FROM records {where} ORDER BY id"
    ).fetchall()
    count = 0
    with store._tx() as conn:
        for row in rows:
            result = predict_text(row["text_redacted"] or "", bundle)
            if result is None:
                continue
            conn.execute(
                """UPDATE records SET ml_ade_probability=?, ml_ade_label=?,
                          ml_model_version=? WHERE id=?""",
                (result["probability"], result["label"], result["model_version"], row["id"]),
            )
            count += 1
    store.close()
    print(f"Scored {count} existing records with {bundle.get('model_version', 'unknown')}")


if __name__ == "__main__":
    main()
