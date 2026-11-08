"""通常入口は旧switchを無視し、保存済み旧modeだけを早期拒否する。"""

import json

import pytest

from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_jobs.integration import sources
from baibai_jobs.maintenance.source_refresh.jquants import JQuantsRefresh


@pytest.mark.parametrize("legacy_env", ["false", "invalid"])
@pytest.mark.parametrize("method", ["ensure_master", "ensure_market_calendar"])
def test_removed_environment_cannot_reenable_sqlite_writer(
    tmp_path, monkeypatch, legacy_env, method
):
    from datetime import date

    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", legacy_env)
    monkeypatch.setenv("BAIBAI_CALENDAR_NATIVE", legacy_env)
    path = tmp_path / "absent.sqlite"
    refresh = JQuantsRefresh(path, None, client=object())
    args = (date(2026, 10, 30),) * (1 if method == "ensure_master" else 2)
    with pytest.raises(JQuantsProviderError, match=r"legacy .* writer is disabled"):
        getattr(refresh, method)(*args)
    assert not path.exists()


@pytest.mark.parametrize("field", ["master_native", "calendar_native"])
@pytest.mark.parametrize("value", [False, None, "true", "false", 1, 0])
def test_fixed_legacy_or_invalid_plan_mode_fails_before_store_reads(tmp_path, field, value):
    from datetime import date

    (tmp_path / "plan.json").write_text(json.dumps({field: value}))
    with pytest.raises(ValueError, match="cutover mode differs"):
        sources.apply(
            object(), tmp_path, market=tmp_path / "absent.sqlite", asof=date(2026, 10, 30)
        )
    assert not (tmp_path / "absent.sqlite").exists()


@pytest.mark.parametrize("fields", [{}, {"master_native": True, "calendar_native": True}])
def test_new_plan_without_mode_or_saved_native_plan_reaches_input_validation(tmp_path, fields):
    from datetime import date

    (tmp_path / "plan.json").write_text(json.dumps({**fields, "handoffs": []}))
    with pytest.raises(ValueError, match="fixed EDINET input is absent"):
        sources.apply(
            object(), tmp_path, market=tmp_path / "absent.sqlite", asof=date(2026, 10, 30)
        )
