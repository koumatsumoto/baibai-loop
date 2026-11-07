"""Master receipts commit before any legacy release can block their progress."""

from datetime import UTC, date, datetime

import pytest

from baibai_jobs.master_cutover import master_native_enabled

DAY = date(2026, 11, 4)
AT = datetime(2026, 11, 4, 3, tzinfo=UTC)


@pytest.mark.parametrize("value", ["TRUE", "1", "yes", " true", "false "])
def test_master_switch_rejects_noncanonical_values(monkeypatch, value):
    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", value)
    with pytest.raises(ValueError, match="must be true or false"):
        master_native_enabled()


def test_native_mode_closes_the_remaining_legacy_bootstrap_master_writer(tmp_path, monkeypatch):
    from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
    from baibai_jobs.maintenance.source_refresh.jquants import JQuantsRefresh

    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", "true")
    refresh = JQuantsRefresh(tmp_path / "execution.sqlite", None)
    monkeypatch.setattr(
        refresh, "_fetch", lambda *_: pytest.fail("old writer must not GET or write")
    )
    with pytest.raises(JQuantsProviderError, match="legacy master writer is disabled"):
        refresh.ensure_master(DAY)
