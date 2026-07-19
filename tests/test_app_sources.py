from __future__ import annotations

from pathlib import Path

from baibai_loop.app.sources.protocols import (
    CandidatesSource,
    LedgerSource,
    ResearchSource,
    TaskSource,
)
from baibai_loop.app.sources.yaml_sources import (
    YamlCandidatesSource,
    YamlLedgerSource,
    YamlResearchSource,
    YamlTaskSource,
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


class TestYamlLedgerSource(LedgerSourceContract):
    def make_source(self, root: Path) -> LedgerSource:
        return YamlLedgerSource(root)


class ResearchSourceContract:
    def make_source(self, root: Path) -> ResearchSource:
        raise NotImplementedError

    def test_revisions_and_packet_detail(self, app_records_root: Path) -> None:
        source = self.make_source(app_records_root)

        revisions = source.revisions()
        detail = source.packet_detail(revisions[0].packet_path)

        assert len(revisions) == 1
        assert revisions[0].ticker == "2331"
        assert revisions[0].review_path is not None
        assert detail.revision == revisions[0]
        assert detail.permanent_loss_risk_count == 7
        assert len(detail.scenarios) == 6

    def test_parse_error_excludes_only_broken_packet(self, app_records_root: Path) -> None:
        broken = app_records_root / "records/03-thesis/2026/07/2026-07-19-9999-decision.yaml"
        broken.write_text("schema_version: broken\n", encoding="utf-8")
        source = self.make_source(app_records_root)

        revisions = source.revisions()

        assert [item.ticker for item in revisions] == ["2331"]
        assert source.load_errors() == ["records/03-thesis/2026/07/2026-07-19-9999-decision.yaml"]


class TestYamlResearchSource(ResearchSourceContract):
    def make_source(self, root: Path) -> ResearchSource:
        return YamlResearchSource(root)


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


class TestYamlTaskSource(TaskSourceContract):
    def make_source(self, root: Path) -> TaskSource:
        return YamlTaskSource(root)


class CandidatesSourceContract:
    def make_source(self, root: Path) -> CandidatesSource:
        raise NotImplementedError

    def test_latest_run_is_none_when_absent(self, tmp_path: Path) -> None:
        assert self.make_source(tmp_path).latest_run() is None

    def test_latest_run_uses_greatest_filename_date(self, app_records_root: Path) -> None:
        source = self.make_source(app_records_root)

        run = source.latest_run()

        assert run is not None
        assert run.run_id == "screening-20260708"
        assert len(run.rows) == 3
        assert run.source_path == "records/02-candidates/2026/07/2026-07-08.yaml"


class TestYamlCandidatesSource(CandidatesSourceContract):
    def make_source(self, root: Path) -> CandidatesSource:
        return YamlCandidatesSource(root)
