from __future__ import annotations

from datetime import date
from pathlib import Path

from tests.helpers.screening_run import screening_candidate, screening_run_payload
from tests.helpers.screening_sqlite import seed_daily_bars

from baibai_engine.market.sqlite import open_connection
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.rules_identity import production_rules_contract_hash
from baibai_engine.screening.run_store import ScreeningRunStore
from baibai_web.sources.db_sources import (
    DbLedgerSource,
    DbMarketPriceSource,
    DbResearchSource,
    DbScreeningSource,
    DbTaskSource,
)
from baibai_web.sources.protocols import (
    LedgerSource,
    ResearchSource,
    ScreeningSource,
    TaskSource,
)


class TestDbLedgerSource:
    def make_source(self, root: Path) -> LedgerSource:
        return DbLedgerSource(root / "stores/application/baibai.sqlite")

    def test_exists_false_when_absent(self, tmp_path: Path) -> None:
        assert self.make_source(tmp_path).exists() is False

    def test_snapshot_totals_and_holdings(self, app_method_root: Path) -> None:
        source = self.make_source(app_method_root)

        snapshot = source.snapshot()

        assert source.exists() is True
        assert snapshot.total_capital_yen == 10_419_500
        assert len(snapshot.holdings) == 1
        assert snapshot.holdings[0].ticker == "2331"


class TestDbResearchSource:
    def make_source(self, root: Path) -> ResearchSource:
        return DbResearchSource(root / "stores/application/baibai.sqlite")

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
        assert source.position_reviews(ticker="2331") == []

    def test_research_triages_returns_the_latest_judgment_for_each_review_set(
        self,
        tmp_path: Path,
        mocker,
    ) -> None:
        read = mocker.patch(
            "baibai_web.sources.db_sources.research_triage_payloads_for_review_set",
            side_effect=[
                [
                    {
                        "research_triage_id": "triage-a",
                        "published_at": "2026-08-28T14:00:00+09:00",
                    },
                    {
                        "research_triage_id": "triage-a-old",
                        "published_at": "2026-08-28T13:00:00+09:00",
                    },
                ],
                [
                    {
                        "research_triage_id": "triage-b",
                        "published_at": "2026-08-28T15:00:00+09:00",
                    }
                ],
            ],
        )
        source = DbResearchSource(tmp_path / "app.sqlite")

        triages = source.research_triages(review_set_ids=["review-set-a", "review-set-b"])

        assert [item["research_triage_id"] for item in triages] == ["triage-b", "triage-a"]
        assert [call.args[1] for call in read.call_args_list] == ["review-set-a", "review-set-b"]


class TestDbTaskSource:
    def make_source(self, root: Path) -> TaskSource:
        return DbTaskSource(root / "stores/application/baibai.sqlite")

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


class TestDbScreeningSource:
    def make_source(self, root: Path) -> ScreeningSource:
        return DbScreeningSource(root / "stores/screening/runs.sqlite")

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

        run = DbScreeningSource(runs_path).latest_run()

        assert run is not None
        assert run.screening_rules_hash == "rules-hash-v1"
        assert run.er_model_version == "expected-return-v1"


class TestDbMarketPriceSource:
    """The daily-delta reads, whose behaviour is `test_market_read_api.py`'s.

    What this owns is the wiring: the source has to hand the market store path to the
    right read. A delegation that lost its argument or called the neighbouring read
    would still type-check and would still answer, with the wrong number.
    """

    def _market(self, tmp_path: Path) -> Path:
        path = tmp_path / "market.sqlite"
        seed_daily_bars(
            path,
            [("2331", "2026-07-28", 100.0, 1.0), ("2331", "2026-07-29", 110.0, 1.0)],
        )
        connection = open_connection(path)
        try:
            connection.executemany(
                "INSERT OR REPLACE INTO jquants_market_calendar(day, is_business_day) "
                "VALUES (?, ?)",
                [("2026-07-28", 1), ("2026-07-29", 1)],
            )
            connection.commit()
        finally:
            connection.close()
        return path

    def test_exists_false_when_absent(self, tmp_path: Path) -> None:
        assert DbMarketPriceSource(tmp_path / "missing.sqlite").exists() is False

    def test_reads_the_previous_trading_day_and_the_change_since_it(self, tmp_path: Path) -> None:
        source = DbMarketPriceSource(self._market(tmp_path))

        previous = source.previous_business_day(date(2026, 7, 29))

        assert source.exists() is True
        assert previous == date(2026, 7, 28)
        assert previous is not None
        assert source.close_changes_since(["2331"], since=previous) == {"2331": 10.0}
        assert source.latest_closes(["2331"]) == {"2331": (110.0, date(2026, 7, 29))}
