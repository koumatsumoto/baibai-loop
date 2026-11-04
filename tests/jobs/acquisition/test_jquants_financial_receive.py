from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime

import pytest

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsAcquisitionError,
    JQuantsPage,
)
from baibai_jobs.acquisition.jquants_financial_receive import receive_financial_days


class Client:
    def __init__(self, *, partial: bool = False) -> None:
        self.partial = partial

    def pages(self, source, params):
        yield JQuantsPage(
            source,
            params,
            datetime(2026, 11, 4, tzinfo=UTC),
            json.dumps(
                {"data": [], **({"pagination_key": "next"} if self.partial else {})}
            ).encode(),
        )
        if self.partial:
            raise JQuantsAcquisitionError("interrupted")


def test_receives_only_new_originals(tmp_path) -> None:
    output = tmp_path / "receipts.sqlite"
    ids = receive_financial_days(output, [date(2026, 11, 2)], client=Client())
    with sqlite3.connect(output) as conn:
        assert conn.execute("SELECT source, query_json FROM jquants_source_pages").fetchall() == [
            ("financial_summaries", '{"date":"20261102"}')
        ]
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name = ?", ("financial_summaries",)
            ).fetchone()
            is None
        )
    assert len(ids) == 1


def test_interrupted_request_keeps_received_page(tmp_path) -> None:
    output = tmp_path / "receipts.sqlite"
    with pytest.raises(JQuantsAcquisitionError):
        receive_financial_days(output, [date(2026, 11, 2)], client=Client(partial=True))
    with sqlite3.connect(output) as conn:
        assert conn.execute("SELECT count(*) FROM jquants_source_pages").fetchone()[0] == 1


def test_partial_handoff_can_be_replayed_without_losing_original(tmp_path) -> None:
    from baibai_jobs.acquisition.jquants_financial_receive import receive_financial_handoff
    from baibai_jobs.integration.source_handoff import _replay

    directory = tmp_path / "handoff"
    ok = receive_financial_handoff(
        directory,
        [date(2026, 11, 2)],
        asof=date(2026, 11, 2),
        started_at=datetime(2026, 11, 4, tzinfo=UTC),
        client=Client(partial=True),
    )
    report = json.loads((directory / "result.json").read_text())
    assert not ok
    assert report["acquisition"]["status"] == "failed"
    assert len(report["request_ids"]) == 1
    canonical = tmp_path / "canonical.sqlite"
    replayed = _replay("jquants-financial", canonical, directory)
    assert not replayed.complete
    with sqlite3.connect(canonical) as conn:
        assert conn.execute("SELECT count(*) FROM jquants_source_pages").fetchone()[0] == 1


def test_completed_handoff_replays_empty_receipt(tmp_path) -> None:
    from baibai_jobs.acquisition.jquants_financial_receive import receive_financial_handoff
    from baibai_jobs.integration.source_handoff import _replay

    directory = tmp_path / "complete"
    assert receive_financial_handoff(
        directory,
        [date(2026, 11, 2)],
        asof=date(2026, 11, 2),
        started_at=datetime(2026, 11, 4, tzinfo=UTC),
        client=Client(),
    )
    canonical = tmp_path / "canonical.sqlite"
    replayed = _replay("jquants-financial", canonical, directory)
    assert replayed.complete
    with sqlite3.connect(canonical) as conn:
        assert conn.execute("SELECT count(*) FROM jquants_source_pages").fetchone()[0] == 1


def test_unavailable_original_blocks_planned_acquisition(tmp_path) -> None:
    from baibai_engine.data_collection.providers.jquants.financial_plan import (
        FinancialAcquisitionPlan,
    )
    from baibai_jobs.acquisition.jquants_financial_plan import (
        FixedFinancialPlan,
        FrozenFinancialPlan,
    )
    from baibai_jobs.acquisition.jquants_financial_receive import receive_planned_handoff

    plan = FinancialAcquisitionPlan((date(2026, 11, 2),), ("stage:unavailable",))
    frozen = FrozenFinancialPlan(
        FixedFinancialPlan("r", "0" * 64, "partial", plan),
        datetime(2026, 11, 4, tzinfo=UTC),
        (),
        ("unavailable",),
    )
    with pytest.raises(ValueError, match="unavailable originals"):
        receive_planned_handoff(tmp_path / "out", frozen, asof=date(2026, 11, 2), client=Client())
    assert not (tmp_path / "out").exists()


def test_l1_acquisition_scheduled_holiday_does_not_fetch(tmp_path, monkeypatch) -> None:
    from baibai_engine.data_collection.providers.jquants.financial_plan import (
        FinancialAcquisitionPlan,
    )
    from baibai_jobs.acquisition import jquants_financial_receive as receiver
    from baibai_jobs.acquisition.jquants_financial_plan import (
        FixedFinancialPlan,
        FrozenFinancialPlan,
    )

    plan = FinancialAcquisitionPlan((date(2026, 11, 2),), ())
    frozen = FrozenFinancialPlan(
        FixedFinancialPlan("r", "0" * 64, "partial", plan),
        datetime(2026, 11, 4, tzinfo=UTC),
        (),
        (),
    )
    monkeypatch.setattr(receiver, "freeze_financial_acquisition_plan", lambda *a, **kw: frozen)
    monkeypatch.setattr(receiver, "available_handoffs", lambda *a, **kw: [])
    monkeypatch.setattr(
        receiver, "retained_snapshot_time", lambda *a: datetime(2026, 11, 3, tzinfo=UTC)
    )
    monkeypatch.setattr(receiver, "iter_fixed_retained_requests", lambda *a, **kw: iter(()))
    monkeypatch.setattr(receiver, "business_day_from_originals", lambda *a: False)
    output = tmp_path / "holiday"
    assert receiver.run_l1_financial_acquisition(
        object(),
        release=object(),
        cache=object(),
        store=object(),
        directory=output,
        asof=date(2026, 11, 2),
        scheduled=True,
        lookback_days=2200,
        revision_overlap_days=7,
        client=Client(),
    )
    report = json.loads((output / "result.json").read_text())
    assert report["acquisition"]["status"] == "skipped_non_business_day"
    assert report["request_ids"] == []
