"""日足の旧planはI/O前に拒否し、環境変数で旧経路へ戻さない。"""

import json
from datetime import date

import pytest
from tests.helpers.edinet_native import empty_edinet_input
from tests.helpers.lake_release import market_store
from tests.helpers.r2_store import MemoryR2Store

from baibai_jobs.integration.sources import apply


@pytest.mark.parametrize("old_setting", [None, "false", "true", "invalid"])
@pytest.mark.parametrize("old_mode", [False, 0, None, "false", "invalid"])
def test_legacy_price_plan_is_rejected_before_io(tmp_path, monkeypatch, old_setting, old_mode):
    if old_setting is None:
        monkeypatch.delenv("BAIBAI_PRICE_NATIVE", raising=False)
    else:
        monkeypatch.setenv("BAIBAI_PRICE_NATIVE", old_setting)
    (tmp_path / "plan.json").write_text(json.dumps({"price_native": old_mode, "handoffs": []}))
    with pytest.raises(ValueError, match="legacy price integration plan"):
        apply(object(), tmp_path, market=tmp_path / "never-open", asof=date(2026, 11, 2))


@pytest.mark.parametrize("old_plan", [False, True])
def test_fixed_native_and_new_fieldless_plans_keep_their_saved_input(tmp_path, old_plan):
    store = MemoryR2Store()
    saved = empty_edinet_input(store, tmp_path / "native").model_dump(mode="json")
    plan = {"handoffs": [], "edinet_input": saved}
    if old_plan:
        plan["price_native"] = True
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    market = market_store(tmp_path / "market.sqlite")
    from tests.helpers.master_calendar_native import seed_calendar, seed_master

    seed_calendar(store, market, tmp_path / "native-calendar", as_of=date(2026, 11, 2))
    seed_master(store, market, tmp_path / "native-master", as_of=date(2026, 11, 2))
    result = apply(store, tmp_path, market=market, asof=date(2026, 11, 2))
    assert result["edinet_input"] == saved
    assert result["handoffs"] == []
