"""Ordinary market acquisition never also acquires legacy daily prices after cutover."""

import json
from datetime import UTC, date, datetime

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_jobs.price_cutover import price_native_enabled

DAY = date(2026, 11, 2)
AT = datetime(2026, 11, 3, tzinfo=UTC)


def test_price_rollout_rejects_invalid_values_and_fixed_plan_mode_mismatch(tmp_path, monkeypatch):
    from baibai_jobs.integration.sources import apply

    monkeypatch.setenv("BAIBAI_PRICE_NATIVE", "TRUE")
    with pytest.raises(ValueError, match="must be true or false"):
        price_native_enabled()
    monkeypatch.setenv("BAIBAI_PRICE_NATIVE", "true")
    (tmp_path / "plan.json").write_text(json.dumps({"price_native": False, "handoffs": []}))
    with pytest.raises(ValueError, match="fixed integration plan"):
        apply(
            MemoryR2Store(),
            tmp_path,
            market=tmp_path / "never-open",
            asof=DAY,
        )
