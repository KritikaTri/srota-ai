import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def seed_db(tmp_path) -> Path:
    """A throw-away copy of the committed seed DB (never touch the original)."""
    dst = tmp_path / "srotaai.db"
    shutil.copy(ROOT / "srotaai_seed.db", dst)
    return dst
