"""
Audit-chain verification for SrotaAI.

Each row in `audit_log` carries:
    hash = sha256(prev_hash | ts | actor | action | target | payload_json)
`verify()` walks the chain in id-order and confirms every row's hash matches
its content + the prior row's hash. Returns (ok, broken_at_id, total).

CLI:
    python -m srotaai.audit srotaai.db
    python -m srotaai.audit srotaai.db --tail 20
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from .store import Store, DEFAULT_DB_PATH


def verify(store: Store) -> tuple[bool, int | None, int]:
    rows = store.conn.execute(
        """SELECT id, ts, actor, action, target, payload_json, prev_hash, hash
           FROM audit_log ORDER BY id ASC"""
    ).fetchall()

    expected_prev = ""
    for r in rows:
        if r["prev_hash"] != expected_prev:
            return False, int(r["id"]), len(rows)
        msg = (
            f"{r['prev_hash']}|{r['ts']}|{r['actor']}|{r['action']}|"
            f"{r['target'] or ''}|{r['payload_json']}"
        )
        h = hashlib.sha256(msg.encode("utf-8")).hexdigest()
        if h != r["hash"]:
            return False, int(r["id"]), len(rows)
        expected_prev = r["hash"]
    return True, None, len(rows)


def _format_tail(store: Store, n: int) -> str:
    rows = store.conn.execute(
        "SELECT id, ts, actor, action, target FROM audit_log ORDER BY id DESC LIMIT ?",
        (n,),
    ).fetchall()
    out = []
    for r in reversed(rows):
        out.append(f"  #{r['id']:<4} {r['ts']}  {r['actor']:<12} "
                   f"{r['action']:<22} target={r['target'] or '-'}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db", nargs="?", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--tail", type=int, default=0,
                    help="also print the last N audit entries")
    args = ap.parse_args()

    store = Store(Path(args.db))
    ok, broken_at, total = verify(store)
    if ok:
        print(f"[OK] audit chain intact across {total} entries  ({args.db})")
    else:
        print(f"[FAIL] audit chain broken at id={broken_at} (of {total})")
        raise SystemExit(2)

    if args.tail:
        print(f"\nLast {args.tail} entries:")
        print(_format_tail(store, args.tail))
    store.close()


if __name__ == "__main__":
    main()
