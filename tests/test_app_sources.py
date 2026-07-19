from __future__ import annotations

from pathlib import Path

from baibai_app.sources.db_sources import (
    DbCandidatesSource,
    DbLedgerSource,
    DbResearchSource,
    DbTaskSource,
)
from baibai_app.sources.protocols import (
    CandidatesSource,
    LedgerSource,
    ResearchSource,
    TaskSource,
)


class LedgerSourceContract:
    def make_source(self, root: Path) -> LedgerSource:
        raise NotImplementedError

    def test_exists_false_when_absent(self, tmp_path: Path) -> None:
        assert self.make_source(tmp_path).exists() is False

    def test_snapshot_totals_and_holdings(self, app_records_root: Path) -> None:
        source = self.make_source(app_records_root)

        snapshot = source.snapshot()

        assert source.exists() is True
        assert snapshot.total_capital_yen == 10_419_500
        assert len(snapshot.holdings) == 1
        assert snapshot.holdings[0].ticker == "2331"


class TestDbLedgerSource(LedgerSourceContract):
    def make_source(self, root: Path) -> LedgerSource:
        return DbLedgerSource(root / "data/app/baibai.sqlite")


class ResearchSourceContract:
    def make_source(self, root: Path) -> ResearchSource:
        raise NotImplementedError

    def test_revisions_and_packet_detail(self, app_records_root: Path) -> None:
        source = self.make_source(app_records_root)

        revisions = source.revisions()
        detail = source.packet_detail(revisions[0].packet_id)

        assert len(revisions) == 1
        assert revisions[0].ticker == "2331"
        assert revisions[0].review_id is not None
        assert detail.revision == revisions[0]
        assert detail.permanent_loss_risk_count == 7
        assert len(detail.scenarios) == 6
        assert source.holding_reviews(ticker="2331") == []


class TestDbResearchSource(ResearchSourceContract):
    def make_source(self, root: Path) -> ResearchSource:
        return DbResearchSource(root / "data/app/baibai.sqlite")


class TaskSourceContract:
    def make_source(self, root: Path) -> TaskSource:
        raise NotImplementedError

    def test_exists_false_and_lists_empty_when_absent(self, tmp_path: Path) -> None:
        source = self.make_source(tmp_path)
        assert source.exists() is False
        assert source.list_tasks() == []

    def test_lists_current_task_states(self, app_records_root: Path) -> None:
        source = self.make_source(app_records_root)

        tasks = source.list_tasks()

        assert source.exists() is True
        assert len(tasks) == 3
        assert [item.status for item in tasks] == ["open", "open", "done"]
        assert tasks[0].event_date is not None


class TestDbTaskSource(TaskSourceContract):
    def make_source(self, root: Path) -> TaskSource:
        return DbTaskSource(root / "data/app/baibai.sqlite")


class CandidatesSourceContract:
    def make_source(self, root: Path) -> CandidatesSource:
        raise NotImplementedError

    def test_latest_run_is_none_when_absent(self, tmp_path: Path) -> None:
        assert self.make_source(tmp_path).latest_run() is None

    def test_latest_run_uses_greatest_run_date(self, app_records_root: Path) -> None:
        source = self.make_source(app_records_root)

        run = source.latest_run()

        assert run is not None
        assert run.run_id == "screening-20260708"
        assert len(run.rows) == 3
        assert run.source_path.startswith("run-revision-")


class TestDbCandidatesSource(CandidatesSourceContract):
    def make_source(self, root: Path) -> CandidatesSource:
        return DbCandidatesSource(
            root / "data/screening/runs.sqlite",
            root / "data/app/baibai.sqlite",
        )
