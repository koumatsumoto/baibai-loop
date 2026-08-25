from __future__ import annotations

from pathlib import Path

from tests.helpers.screening_run import screening_candidate, screening_run_payload

from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.rules_identity import production_rules_contract_hash
from baibai_engine.screening.run_store import ScreeningRunStore
from baibai_web.sources.db_sources import (
    DbCandidatesSource,
    DbLedgerSource,
    DbResearchSource,
    DbTaskSource,
)
from baibai_web.sources.protocols import (
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

    def test_snapshot_totals_and_holdings(self, app_method_root: Path) -> None:
        source = self.make_source(app_method_root)

        snapshot = source.snapshot()

        assert source.exists() is True
        assert snapshot.total_capital_yen == 10_419_500
        assert len(snapshot.holdings) == 1
        assert snapshot.holdings[0].ticker == "2331"


class TestDbLedgerSource(LedgerSourceContract):
    def make_source(self, root: Path) -> LedgerSource:
        return DbLedgerSource(root / "stores/application/baibai.sqlite")


class ResearchSourceContract:
    def make_source(self, root: Path) -> ResearchSource:
        raise NotImplementedError

    def test_revisions_and_thesis_detail(self, app_method_root: Path) -> None:
        source = self.make_source(app_method_root)

        revisions = source.revisions()
        detail = source.thesis_detail(revisions[0].thesis_id)

        assert len(revisions) == 1
        assert revisions[0].ticker == "2331"
        assert revisions[0].review_id is not None
        assert detail.revision == revisions[0]
        assert detail.permanent_loss_risk_count == 7
        assert len(detail.scenarios) == 6
        assert source.holding_reviews(ticker="2331") == []


class TestDbResearchSource(ResearchSourceContract):
    def make_source(self, root: Path) -> ResearchSource:
        return DbResearchSource(root / "stores/application/baibai.sqlite")


class TaskSourceContract:
    def make_source(self, root: Path) -> TaskSource:
        raise NotImplementedError

    def test_exists_false_and_lists_empty_when_absent(self, tmp_path: Path) -> None:
        source = self.make_source(tmp_path)
        assert source.exists() is False
        assert source.list_tasks() == []

    def test_lists_current_task_states(self, app_method_root: Path) -> None:
        source = self.make_source(app_method_root)

        tasks = source.list_tasks()

        assert source.exists() is True
        assert len(tasks) == 3
        assert [item.status for item in tasks] == ["open", "open", "done"]
        assert tasks[0].event_date is not None


class TestDbTaskSource(TaskSourceContract):
    def make_source(self, root: Path) -> TaskSource:
        return DbTaskSource(root / "stores/application/baibai.sqlite")


class CandidatesSourceContract:
    def make_source(self, root: Path) -> CandidatesSource:
        raise NotImplementedError

    def test_latest_run_is_none_when_absent(self, tmp_path: Path) -> None:
        assert self.make_source(tmp_path).latest_run() is None

    def test_latest_run_uses_greatest_run_date(self, app_method_root: Path) -> None:
        source = self.make_source(app_method_root)

        run = source.latest_run()

        assert run is not None
        assert run.run_id == "screening-20260708"
        assert len(run.rows) == 3
        assert run.run_revision_id.startswith("run-revision-")
        assert run.screening_rules_hash == production_rules_contract_hash(
            load_screening_rules().model_dump_json()
        )
        assert run.er_model_version == "expected-return-v1"


class TestDbCandidatesSource(CandidatesSourceContract):
    def make_source(self, root: Path) -> CandidatesSource:
        return DbCandidatesSource(
            root / "stores/screening/runs.sqlite",
            root / "stores/application/baibai.sqlite",
        )

    def test_latest_run_preserves_method_identity(self, tmp_path: Path) -> None:
        runs_path = tmp_path / "runs.sqlite"
        ScreeningRunStore(runs_path).publish_run(
            screening_run_payload(
                as_of="2026-08-01",
                run_at="2026-08-01T18:30:00+09:00",
                universe_size=1,
                rules_hash="rules-hash-v1",
                candidates=[screening_candidate("4432", name="sample")],
            )
        )

        run = DbCandidatesSource(runs_path, tmp_path / "app.sqlite").latest_run()

        assert run is not None
        assert run.screening_rules_hash == "rules-hash-v1"
        assert run.er_model_version == "expected-return-v1"
