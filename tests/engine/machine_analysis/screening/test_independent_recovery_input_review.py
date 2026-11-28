"""Supported recovery may need price-basis research without a financial match."""

from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.model_generation import materialize_generation, publish_generation

from baibai_engine.investment_judgment.workflow.discovery import discovery_context
from baibai_engine.investment_judgment.workflow.selection import ResearchRequest
from baibai_engine.machine_analysis.generation.assembly import CompanyModelInputs, generate_models
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.models.technical_analysis.price_path import observe_price_path
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.observations.prices.price_observation import price_calendar
from baibai_engine.machine_analysis.screening.discovery_contract import discovery_publication_id
from baibai_engine.machine_analysis.screening.discovery_index import read_discovery_index
from baibai_engine.machine_analysis.screening.policies.discovery import (
    evaluate_discovery_rows,
    project_discovery_row,
)
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore

_AS_OF = date(2025, 8, 29)
_TICKER = "1001"
_RELEASE = InputRelease(release_id="native-recovery-review", manifest_sha256="b" * 64)


def _generation(
    *,
    quote="current",
    treasury_shares=None,
    sector="情報・通信業",
    common_stock=True,
    margin_improved=True,
    prior_standard="JP",
    growth_match=False,
):
    # Flat sales and loss narrowing; negative net assets mean G's independent
    # balance condition is known false, not a failed positive-price test.
    periods = (
        (
            2024,
            "1Q",
            250.0 if margin_improved else 1000.0,
            10.0 if growth_match else -30.0,
            15.0 if growth_match else -32.0,
        ),
        (2024, "FY", 1200.0, 100.0 if growth_match else -100.0, 100.0 if growth_match else -110.0),
        (
            2025,
            "1Q",
            400.0 if growth_match else 250.0,
            30.0 if growth_match else -10.0,
            25.0 if growth_match else -12.0,
        ),
    )
    docs = []
    for i, (year, fiscal_period, sales, operating_profit, profit) in enumerate(periods):
        end = date(year + 1, 3, 31) if fiscal_period == "FY" else date(year, 6, 30)
        standard = prior_standard if not i else "JP"
        docs.append(
            JQuantsFinancialSummary(
                ticker=_TICKER,
                disclosed_at=end + timedelta(days=40),
                fiscal_period=fiscal_period,
                fiscal_year_end=date(year + 1, 3, 31),
                period_start=date(year, 4, 1),
                period_end=end,
                sales=sales,
                operating_profit=operating_profit,
                profit=profit,
                shares_outstanding=100.0,
                treasury_shares=treasury_shares,
                equity=500.0 if growth_match else -50.0,
                disclosure_number=f"native-recovery-{year}-{fiscal_period}",
                document_type=f"{fiscal_period}FinancialStatements_Consolidated_{standard}",
            )
        )
    prior_day = _AS_OF - timedelta(days=1)
    calendar = price_calendar({prior_day: True, _AS_OF: True}, as_of=_AS_OF)
    quote_day = prior_day if quote == "stale" else _AS_OF
    bars = () if quote == "none" else (PriceBar(quote_day, 10.0, None),)
    return materialize_generation(
        generate_models(
            generation_id="native-recovery-review",
            as_of=_AS_OF,
            evaluation_time=datetime(2025, 8, 31, tzinfo=UTC),
            inputs={
                "market": InputSnapshot(
                    kind="lake_release",
                    reference=_RELEASE.release_id,
                    sha256=_RELEASE.manifest_sha256,
                )
            },
            financial_release=_RELEASE,
            subjects=(_TICKER,),
            companies=(
                CompanyModelInputs(
                    ticker=_TICKER,
                    financial_documents=docs,
                    adjusted_bars=bars,
                    price_path=observe_price_path(
                        bars,
                        calendar=calendar,
                        day_states={quote_day: "priced"} if bars else {},
                    ).payload,
                    adjustment_events=(),
                    sector=sector,
                    market_context={
                        "master": {"sector_33": sector, "is_common_stock": common_stock},
                        "price": {
                            "close": bars[0].price if bars else None,
                            "price_basis": "price_adjusted_close",
                            "resolved_date": quote_day.isoformat() if bars else None,
                        },
                    },
                ),
            ),
            trading_dates=calendar.trading_dates,
            expected_price_date=_AS_OF,
            ttm_rules=DEFAULT_TTM_RULES,
            external_observations={},
            market_context=None,
            market_context_revision="native-recovery-review-fixture",
        )
    )


@pytest.mark.parametrize(
    ("quote", "treasury_shares", "reason"),
    [
        ("current", 20.0, None),
        ("current", None, "missing_capital_basis"),
        ("stale", 20.0, "stale_price"),
    ],
)
def test_native_recovery_reaches_input_gap_without_inventing_a_match(
    tmp_path, quote, treasury_shares, reason
):
    fixed = _generation(quote=quote, treasury_shares=treasury_shares)
    assert (
        fixed.result("company_financials", _TICKER).payload["earnings_price"]["unavailable_reason"]
        == reason
    )
    row = project_discovery_row(_TICKER, models=fixed.models, metadata=fixed)
    assert row["financial_state"] == "not_selected"
    assert row["price_ready"] is (quote == "current")
    assert row["matched_paths"]["growth"] == []
    assert row["matched_paths"]["financial"] == ([] if reason else ["operating_recovery"])
    assert row["screening_lane"] == ("input_review" if reason else "alternative_financial_research")
    assert row["missing"] == ([f"financial:{reason}"] if reason else [])
    if reason:
        assert row["coordinates"] == []

    path = tmp_path / "runs"
    publish_generation(fixed, path=path)
    selection = evaluate_discovery_rows(fixed, tickers=(_TICKER,), rows={_TICKER: row})
    identity = discovery_publication_id(fixed.generation_id)
    assert ScreeningSelectionStore(path).publish(identity, selection)
    known_at = datetime.now(UTC)
    index = read_discovery_index(path=path, selection_id=identity, as_of=_AS_OF, known_at=known_at)
    assert index.rows == (row,)
    result = discovery_context(
        index,
        request=ResearchRequest(scope="discover_new", text="test independent recovery"),
        as_of=_AS_OF,
        known_at=known_at,
        path=path,
        db_path=None,
        question="input_gap",
        page=0,
        comparisons=None,
        minimum=None,
    )
    assert result["discovery"]["total"] == int(reason is not None)
    assert result["discovery"]["rows"] == (
        [
            {
                "ticker": _TICKER,
                "observations": row,
                "detail_args": ["--compare-ticker", _TICKER],
            }
        ]
        if reason
        else []
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"margin_improved": False},
        {"prior_standard": "IFRS"},
        {"sector": "その他"},
        {"common_stock": False},
        {"quote": "none"},
        {"growth_match": True},
    ],
)
def test_recovery_gap_does_not_expand_scope_or_override_matched_growth(overrides):
    fixed = _generation(**overrides)
    row = project_discovery_row(_TICKER, models=fixed.models, metadata=fixed)
    assert not any(reason.startswith("financial:") for reason in row["missing"])
    assert row["coordinates"] == []
    assert row["matched_paths"]["financial"] == []
    assert row["screening_lane"] == (
        "growth_research" if overrides.get("growth_match") else "no_current_hypothesis"
    )
