"""Master receipts commit before any legacy release can block their progress."""

from datetime import UTC, date, datetime

import pytest

DAY = date(2026, 11, 4)
AT = datetime(2026, 11, 4, 3, tzinfo=UTC)


def test_native_mode_closes_the_remaining_legacy_bootstrap_master_writer(tmp_path, monkeypatch):
    from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
    from baibai_jobs.maintenance.source_refresh.jquants import JQuantsRefresh

    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", "false")
    refresh = JQuantsRefresh(tmp_path / "execution.sqlite", None)
    with pytest.raises(JQuantsProviderError, match="legacy master writer is disabled"):
        refresh.ensure_master(DAY)
