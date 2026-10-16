from __future__ import annotations

from contextlib import closing
from datetime import date, datetime
from pathlib import Path

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.db_seed import seed_ledger
from tests.helpers.screening_run import RULES_HASH, screening_run_payload, stock_analysis
from tests.helpers.screening_sqlite import seed_daily_bars
from tests.web.test_research_assessment_views import seed_web_assessment, seed_web_proposal

from baibai_engine.position.ledger import PortfolioLedgerDocument
from baibai_engine.position.valuation import current_portfolio
from baibai_engine.research.assessment_store import AssessmentStoreService
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.rules_identity import production_rules_contract_hash
from baibai_engine.screening.run_store import ScreeningRunStore
from baibai_engine.storage.application.write import connect_rw
from baibai_engine.storage.observations.schema import open_connection
from baibai_web.readmodel.builders import build_dashboard
from baibai_web.readmodel.stocks.detail import build_security_detail
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
        return DbLedgerSource(
            root / "stores/application/baibai.sqlite", root / "stores/market/market.sqlite"
        )

    def test_exists_false_when_absent(self, tmp_path: Path) -> None:
        assert self.make_source(tmp_path).exists() is False

    def test_snapshot_totals_and_holdings(self, app_method_root: Path) -> None:
        source = self.make_source(app_method_root)

        snapshot = source.snapshot()

        assert source.exists() is True
        assert snapshot.total_capital_yen is None
        assert len(snapshot.holdings) == 1
        assert snapshot.holdings[0].ticker == "2331"


class TestDbResearchSource:
    def make_source(self, root: Path) -> ResearchSource:
        return DbResearchSource(root / "stores/application/baibai.sqlite")

    def test_revisions_and_exact_assessment_detail(self, app_method_root: Path) -> None:
        source = self.make_source(app_method_root)
        revisions = source.revisions()
        detail = source.assessment_detail(revisions[0].assessment_id)
        assert len(revisions) == 1
        assert revisions[0].ticker == "2331"
        assert revisions[0].review_id
        assert detail.revision == revisions[0]
        assert detail.projection["status"] == "resolved"
        assert detail.document["schema_version"] == 1
        assert detail.document["permanent_loss_risks"]
        assert detail.review["review_id"] == revisions[0].review_id
        assert source.proposals() == []
        assert (
            source.assessment_detail("stock-assessment-00000000-0000-4000-8000-00000000ffff")
            is None
        )

    def test_holding_proposal_keeps_case_judgment_and_unavailable_return(
        self, app_method_root: Path
    ) -> None:
        _, _, pair, proposal = seed_web_proposal(
            app_method_root,
            tickers=("2331",),
            scope="review_positions",
            actions={"2331": "hold"},
        )
        source = self.make_source(app_method_root)
        stored = source.proposal(proposal.proposal_id)
        item = stored["items"][0]
        assert item["action"] == "hold"
        assert item["holding_basis"] == "investment_case_intact"
        assert item["remaining_reward"] is None
        assert item["return_calculation_unavailable_reason"]
        assert item["assessment_ref"]["assessment_id"] == pair.assessment_id
        detail = build_security_detail(
            "2331",
            DbLedgerSource(
                app_method_root / "stores/application/baibai.sqlite",
                app_method_root / "stores/market/market.sqlite",
            ),
            source,
            DbScreeningSource(app_method_root / "stores/screening/runs.sqlite"),
            DbMarketPriceSource(app_method_root / "stores/market/market.sqlite"),
        )
        assert detail.investment_proposals[0].actions == ["hold"]
        assert detail.latest_assessment.revision.assessment_id == pair.assessment_id

    def test_missing_exact_review_is_a_source_error_without_older_fallback(
        self, app_method_root: Path
    ) -> None:
        pair = seed_web_assessment(app_method_root, ticker="2331")
        db = app_method_root / "stores/application/baibai.sqlite"
        with closing(connect_rw(db)) as connection, connection:
            connection.execute(
                "DELETE FROM stock_assessment_review WHERE assessment_id=?", (pair.assessment_id,)
            )
        source = self.make_source(app_method_root)
        with pytest.raises(ValueError, match="Review"):
            source.revisions()
        with pytest.raises(ValueError, match="Review"):
            source.assessment_detail(pair.assessment_id)


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
                rules_hash=RULES_HASH,
                stock_analyses=[stock_analysis("4432", name="sample")],
            ),
            rules=load_screening_rules(),
        )

        run = DbScreeningSource(runs_path).latest_run()

        assert run is not None
        assert run.screening_rules_hash == RULES_HASH
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
                "INSERT OR REPLACE INTO jquants_market_calendar(day, holiday_division) "
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


def test_assessment_snapshot_projection_retains_original_basis_and_review(
    app_method_root,
):
    pair = seed_web_assessment(app_method_root, ticker="2331")
    source = DbResearchSource(app_method_root / "stores/application/baibai.sqlite")
    detail = source.assessment_detail(pair.assessment_id)
    assert detail.revision.stance == "supported"
    assert detail.projection["as_of"] == "2026-10-14"
    assert detail.projection["original_quote_at"] == "2026-10-14T15:30:00+09:00"
    assert detail.projection["original_price_basis"] == "last_close_unadjusted"
    assert detail.projection["projections"]["base"]["annualized_return_pct"] is not None
    assert detail.projection["horizon_months"] == 12
    assert detail.projection["raw"] == pair.document.model_dump(mode="json")
    assert detail.review["review_id"] == pair.review.review_id


@pytest.mark.parametrize("factor", [0.5, None, 1.0, "missing_original"])
def test_holding_pmax_gap_checks_the_assessment_window_even_when_acquired_after_split(
    tmp_path: Path, mocker, factor
) -> None:
    db, market = tmp_path / "app.sqlite", tmp_path / "market.sqlite"
    now = datetime.fromisoformat("2026-10-14T17:00:00+09:00")
    document, review = pair_payload(ticker="1234", quote_as_of="2026-10-12")
    document["valuation"]["required_annual_return_pct"] = "6"
    document["valuation"]["base"].update(
        terminal_value_per_share_yen="2120", cash_distribution_per_share_yen="0"
    )
    review["recalculated_projections"][0].update(
        terminal_value_per_share_yen="2120", cash_distribution_per_share_yen="0"
    )
    update_review_hash(document, review)
    AssessmentStoreService(db, clock=lambda: now).publish_reviewed_assessment(
        assessment_id(), document, review
    )
    ledger = PortfolioLedgerDocument.model_validate(
        {
            "schema_version": 3,
            "portfolio_scope": "repository_only",
            "as_of": now.isoformat(),
            "events": [
                {
                    "type": "opening_balance",
                    "event_id": "opening",
                    "occurred_at": "2026-10-14T08:00:00+09:00",
                    "amount_yen": 1000000,
                },
                {
                    "type": "reservation",
                    "event_id": "reserve-after-split",
                    "occurred_at": "2026-10-14T08:30:00+09:00",
                    "reservation_id": "after-split",
                    "order_id": "order-after-split",
                    "ticker": "1234",
                    "sector": "未分類",
                    "common_factors": [],
                    "quantity": 100,
                    "price_guard_yen": 1000,
                    "expires_at": "2026-10-14T15:30:00+09:00",
                },
                {
                    "type": "execution",
                    "event_id": "buy-after-split",
                    "execution_id": "buy",
                    "reservation_id": "after-split",
                    "occurred_at": "2026-10-14T09:00:00+09:00",
                    "ticker": "1234",
                    "side": "buy",
                    "quantity": 100,
                    "price_yen": 1000,
                },
            ],
        }
    )
    seed_ledger(db, ledger)
    seed_daily_bars(
        market,
        [
            ("1234", "2026-10-12", 2000.0, 1.0),
            ("1234", "2026-10-13", 1000.0, 1.0 if factor == "missing_original" else factor),
            ("1234", "2026-10-14", 1000.0, 1.0),
        ],
    )
    if factor == "missing_original":
        with closing(open_connection(market)) as connection, connection:
            connection.execute(
                "DELETE FROM jquants_daily_bars WHERE ticker='1234' AND traded_at='2026-10-12'"
            )
    snapshot = current_portfolio(ledger, sqlite_path=market, now=now)
    assert snapshot.holdings[0].market_price_yen == 1000
    ledger_source = mocker.Mock(spec=LedgerSource)
    ledger_source.exists.return_value = True
    ledger_source.snapshot.return_value = snapshot
    screening = mocker.Mock(spec=ScreeningSource)
    screening.latest_run.return_value = None
    before = {path: path.read_bytes() for path in (db, market)}

    view = build_dashboard(
        ledger_source, DbResearchSource(db), screening, DbMarketPriceSource(market)
    )

    holding = view.holdings[0]
    assert holding.pmax_raw_yen == 2000
    if factor == 1.0:
        assert holding.pmax_gap_pct == 100
        assert holding.pmax_gap_unavailable_reason is None
    else:
        assert holding.pmax_gap_pct is None
        assert holding.pmax_gap_unavailable_reason == "原評価と現在株価を同じ価格基準で比較できません。"
    assert {path: path.read_bytes() for path in (db, market)} == before
