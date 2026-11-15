"""Completed capital events do not get resolved by unrelated capital observations."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.financial_documents import document

from baibai_engine.machine_analysis.generation.contracts import InputSnapshot, ModelGeneration
from baibai_engine.machine_analysis.generation.financial_models import (
    generate_financial_and_price_models,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.financial_snapshot import (
    build_financial_snapshot,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.observations.financials.capital_action_pdf import CapitalAction
from baibai_engine.machine_analysis.observations.financials.financial_documents import (
    normalize_financial_summary,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar

ASOF = date(2026, 10, 13)
ACTION = CapitalAction(
    ticker="9999",
    issued_shares=130,
    effective_date=date(2026, 8, 15),
    received_at=datetime(2026, 8, 16, tzinfo=UTC),
    source_url="https://example.invalid/completed.pdf",
    source_sha256="a" * 64,
    disclosure_number="completed-event",
    description="completed",
    published_on=date(2026, 8, 15),
)


def documents(*, complete_capital=False, forecast_day=None, eps=20):
    records = [
        document(
            "old-annual",
            day="2026-08-01",
            DocType="FYFinancialStatements_Consolidated_JP",
            CurFYSt="2025-07-01",
            CurFYEn="2026-06-30",
            CurPerSt="2025-07-01",
            CurPerEn="2026-06-30",
            NxtFYSt="2026-07-01",
            NxtFYEn="2027-06-30",
            NxFEPS=str(eps),
            NxFNp=str(eps * 80),
            NxFOdP="1500",
            NP="800",
            Sales="5000",
            ShOutFY="100",
            TrShFY="20",
        )
    ]
    if complete_capital:
        records.append(
            document(
                "later-capital",
                day="2026-10-10",
                DocType="1QFinancialStatements_Consolidated_JP",
                CurPerType="1Q",
                CurPerSt="2026-07-01",
                CurPerEn="2026-09-30",
                NP="100",
                Sales="1000",
                ShOutFY="130",
                TrShFY="22",
            )
        )
    if forecast_day:
        records.append(
            document("new-forecast", day=forecast_day, FEPS="12", FNP="1296", FOdP="1200")
        )
    rows = [normalize_financial_summary(record) for record in records]
    assert all(row is not None for row in rows)
    return [row for row in rows if row is not None]


def snapshot(rows, actions):
    return build_financial_snapshot(
        100,
        rows,
        None,
        DEFAULT_TTM_RULES,
        adjustment_events=(),
        asof_date=ASOF,
        capital_actions=actions,
    )


@pytest.mark.parametrize("capital_state", ["unresolved", "resolved", "conflicting"])
@pytest.mark.parametrize("eps", [20, 0, -20])
def test_pre_event_eps_does_not_revive_when_capital_basis_changes(capital_state, eps):
    rows = documents(complete_capital=capital_state == "resolved", eps=eps)
    before = tuple(rows)
    actions = [ACTION]
    if capital_state == "conflicting":
        actions.append(replace(ACTION, issued_shares=140, source_sha256="b" * 64))
    result = snapshot(rows, actions)
    assert result.forecast_eps_yen is None
    assert result.per_forward is None
    assert result.forecast_profit_yen == eps * 80
    assert result.forecast_ordinary_profit_yen == 1500
    assert result.earnings_forecast_source["disclosure_number"] == "old-annual"
    assert result.earnings_forecast_source["disclosed_at"] == "2026-08-01"
    assert result.earnings_forecast_source["period_end"] == "2027-06-30"
    assert tuple(rows) == before
    if capital_state == "resolved":
        assert result.shares_ex_treasury == 108
        assert result.market_cap == 10800
        assert result.capital_basis_failure_reason is None
    else:
        assert result.market_cap is None
        assert result.capital_basis_failure_reason == (
            "indeterminate_treasury_after_capital_action"
            if capital_state == "unresolved"
            else "conflicting_capital_actions"
        )


@pytest.mark.parametrize("forecast_day", ["2026-08-15", "2026-10-11"])
def test_on_or_after_event_forecast_remains_usable(forecast_day):
    result = snapshot(documents(complete_capital=True, forecast_day=forecast_day), [ACTION])
    assert result.forecast_eps_yen == 12
    assert result.per_forward == 100 / 12
    assert result.forecast_profit_yen == 1296
    assert result.earnings_forecast_source["disclosure_number"] == "new-forecast"
    assert result.shares_ex_treasury == 108
    assert result.market_cap == 10800


def test_without_completed_event_existing_forecast_is_not_discarded():
    result = snapshot(documents(complete_capital=True), [])
    assert result.forecast_eps_yen == 20
    assert result.per_forward == 5
    assert result.market_cap == 10800


def test_normal_model_generation_keeps_current_capital_but_not_old_forward_multiple():
    models = generate_financial_and_price_models(
        ticker="9999",
        documents=documents(complete_capital=True),
        adjusted_bars=[PriceBar(ASOF, 100, None)],
        adjustment_events=(),
        trading_dates=tuple(ASOF - timedelta(days=252 - index) for index in range(253)),
        expected_price_date=ASOF,
        sector=None,
        as_of=ASOF,
        input_release=InputRelease(release_id="synthetic", manifest_sha256="c" * 64),
        ttm_rules=DEFAULT_TTM_RULES,
        capital_actions=[ACTION],
    )
    generation = ModelGeneration(
        generation_id="synthetic-capital-forecast",
        as_of=ASOF,
        evaluation_time=datetime(2026, 10, 14, tzinfo=UTC),
        inputs={
            "market": InputSnapshot(kind="lake_release", reference="synthetic", sha256="c" * 64)
        },
        models=models,
    )
    saved = ModelGeneration.model_validate_json(generation.model_dump_json())
    payload = saved.result("company_financials", "9999").payload
    financial = payload["financial"]
    assert financial["forecast_eps_yen"] is None
    assert financial["per_forward"] is None
    assert financial["forecast_profit_yen"] == 1600
    assert financial["market_cap"] == 10800
    assert financial["earnings_forecast_source"]["disclosed_at"] == "2026-08-01"
    assert payload["capital_action_evidence"][0]["issued_shares"] == 130
    assert payload["capital_action_evidence"][0]["source_sha256"] == ACTION.source_sha256
