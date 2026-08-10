"""The capital-control annotation must reach the judgment surface and nothing else.

Every earlier event-delta signal measured negative over 3y/5y, so these facts are
supplied to the human and kept out of the machine. That is only true while nothing in
the screen, the estimate or the selection reads them, which a reviewer cannot see by
looking at the annotation itself — hence this regression.
"""

from __future__ import annotations

from datetime import date

from baibai_engine.screening.candidate_build import build_screened_candidate
from baibai_engine.screening.capital_control import CapitalControlAnnotation
from baibai_engine.screening.render import candidate_entry
from baibai_engine.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules
from baibai_engine.screening.schema import (
    DerivedMetrics,
    FinancialSnapshot,
    ScreenedCandidate,
    SecurityMaster,
    UniverseSnapshot,
)
from baibai_engine.screening.selection import build_selection_payload, candidate_record_from_mapping

_ASOF = date(2026, 7, 29)

ANNOTATION_KEYS = frozenset(
    {
        "tse_capital_policy_status",
        "tse_capital_policy_updated_on",
        "large_holding_event_recent",
        "large_holding_event_latest_on",
        "tender_offer_event_recent",
        "tender_offer_event_latest_on",
    }
)

_ANNOTATION = CapitalControlAnnotation(
    tse_capital_policy_status="disclosed",
    tse_capital_policy_updated_on=date(2026, 6, 23),
    large_holding_event_recent=True,
    large_holding_event_latest_on=date(2026, 7, 1),
    tender_offer_event_recent=True,
    tender_offer_event_latest_on=date(2026, 5, 20),
)


def _candidate(
    ticker: str, pbr: float, *, capital_control: CapitalControlAnnotation | None
) -> ScreenedCandidate:
    return build_screened_candidate(
        ticker=ticker,
        security=SecurityMaster(
            code=ticker,
            name=f"company-{ticker}",
            market_segment="Prime",
            sector_33="機械",
            is_common_stock=True,
        ),
        financial=FinancialSnapshot(
            latest_disclosed_at=None,
            per_forward=None,
            per_trailing=None,
            pbr=pbr,
            ev_ebitda=None,
            p_s=None,
            pcfr=None,
            eps=None,
            sales_ttm=None,
            ocf_ttm=None,
            market_price_yen=100.0,
            shares_outstanding=100_000_000.0,
            shares_ex_treasury=90_000_000.0,
            market_cap=9_000_000_000.0,
        ),
        derived=DerivedMetrics(sector_median_value={"pbr": 1.2}),
        universe_snapshot=UniverseSnapshot(
            market_cap_oku=500,
            avg_turnover_oku=2.0,
            listing_span_days=1_200,
        ),
        evidence_hits=(),
        capital_control=capital_control,
    )


def _payload(candidates: tuple[ScreenedCandidate, ...]) -> dict[str, object]:
    return build_selection_payload(
        asof_date=_ASOF,
        candidates=tuple(
            candidate_record_from_mapping(candidate_entry(candidate)) for candidate in candidates
        ),
        macro_context=None,
        rules=load_screening_rules(DEFAULT_RULES_PATH),
        top=10,
        profile="balanced",
        candidates_ref="test.yaml",
        macro_context_ref=None,
        longlist_top=5,
    )


def test_only_the_six_annotation_fields_change_when_the_annotation_is_present() -> None:
    without = candidate_entry(_candidate("1111", 0.8, capital_control=None))
    with_annotation = candidate_entry(_candidate("1111", 0.8, capital_control=_ANNOTATION))

    metrics_without = dict(without.pop("metrics"))  # type: ignore[arg-type]
    metrics_with = dict(with_annotation.pop("metrics"))  # type: ignore[arg-type]

    assert without == with_annotation
    assert {
        key for key in metrics_with if metrics_with[key] != metrics_without.get(key)
    } == ANNOTATION_KEYS


def test_selection_rank_expected_return_and_gate_do_not_move() -> None:
    candidates = (
        _candidate("1111", 0.6, capital_control=None),
        _candidate("2222", 0.9, capital_control=None),
    )
    annotated = (
        _candidate("1111", 0.6, capital_control=_ANNOTATION),
        # The other name gets a different annotation so a leak would reorder them.
        _candidate(
            "2222",
            0.9,
            capital_control=CapitalControlAnnotation(
                tse_capital_policy_status="none",
                tse_capital_policy_updated_on=None,
                large_holding_event_recent=False,
                large_holding_event_latest_on=None,
                tender_offer_event_recent=True,
                tender_offer_event_latest_on=date(2026, 7, 9),
            ),
        ),
    )

    baseline = _payload(candidates)
    with_annotation = _payload(annotated)

    def _comparable(payload: dict[str, object]) -> list[tuple[object, ...]]:
        rows = payload["longlist"]
        assert isinstance(rows, list)
        return [
            (row["rank"], row["ticker"], row["expected_return_pct"], row["liquidity_status"])
            for row in rows
        ]

    assert _comparable(baseline) == _comparable(with_annotation)
    assert [row["ticker"] for row in baseline["recommendations"]] == [  # type: ignore[union-attr]
        row["ticker"]
        for row in with_annotation["recommendations"]  # type: ignore[union-attr]
    ]


def test_the_annotation_reaches_the_judgment_surface() -> None:
    payload = _payload((_candidate("1111", 0.6, capital_control=_ANNOTATION),))

    recommendation = payload["recommendations"][0]  # type: ignore[index]
    longlist = payload["longlist"][0]  # type: ignore[index]

    assert recommendation["tse_capital_policy_status"] == "disclosed"
    assert recommendation["large_holding_event_latest_on"] == "2026-07-01"
    assert recommendation["tender_offer_event_latest_on"] == "2026-05-20"
    assert longlist["capital_control"]["tse_capital_policy_updated_on"] == "2026-06-23"
