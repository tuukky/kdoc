"""모든 테스트는 임시 운영 DB를 쓴다 (data/ops/kdoc.db 를 건드리지 않음)."""

import pytest


@pytest.fixture(autouse=True)
def _ops_db(tmp_path, monkeypatch):
    monkeypatch.setenv("KDOC_OPS_DB", str(tmp_path / "ops.db"))
