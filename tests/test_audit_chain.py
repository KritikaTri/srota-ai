import sqlite3
from pathlib import Path

from srotaai.audit import verify
from srotaai.store import Store


def test_committed_seed_db_chain_is_intact(seed_db):
    """Regression: the seed DB once shipped with a forked audit chain."""
    store = Store(Path(seed_db))
    ok, broken_at, total = verify(store)
    store.close()
    assert ok, f"audit chain broken at id={broken_at}"
    assert total > 0


def test_appends_stay_chained_and_tamper_is_detected(seed_db):
    store = Store(Path(seed_db))
    store.append_audit("test", "unit.append", target="t1", payload={"k": 1})
    store.append_audit("test", "unit.append", target="t2")
    assert verify(store)[0] is True
    store.close()

    conn = sqlite3.connect(seed_db)
    conn.execute("UPDATE audit_log SET actor='mallory' WHERE id = "
                 "(SELECT MAX(id) - 1 FROM audit_log)")
    conn.commit()
    conn.close()

    store = Store(Path(seed_db))
    ok, broken_at, _ = verify(store)
    store.close()
    assert ok is False and broken_at is not None
