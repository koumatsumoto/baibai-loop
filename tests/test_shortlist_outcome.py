from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_engine.screening.calibration.forward import ForwardReturnRow
from baibai_engine.screening.shortlist_outcome import (
    cohort_from_payload,
    evaluate_cohort,
    with_machine_estimates,
)

AS_OF = date(2026, 1, 30)


def _entry(ticker: str, decision: str, *, ploss: str | None = None) -> dict[str, object]:
    entry: dict[str, object] = {"ticker": ticker, "decision": decision, "reason": "…"}
    if decision == "selected":
        entry["rank"] = 1
        entry["narrative"] = {"ploss": ploss or "中低", "catalyst_date": "2026-02-14"}
    return entry


def _payload(entries: list[dict[str, object]]) -> dict[str, object]:
    return {
        "shortlist_id": "shortlist-20260130-a",
        "as_of": AS_OF,
        "run_revision_id": "run-revision-a",
        "entries": entries,
    }


def _forward(ticker: str, price_return: float | None) -> ForwardReturnRow:
    return ForwardReturnRow(
        asof=AS_OF.isoformat(),
        ticker=ticker,
        horizon="3m",
        target_date="2026-04-30",
        resolved=price_return is not None,
        price_return=price_return,
        stale_price=False,
        entry_date=AS_OF.isoformat(),
        exit_date="2026-04-30" if price_return is not None else None,
        status="resolved" if price_return is not None else "unresolved_future_horizon",
    )


def test_cohort_reads_the_verdict_and_the_permanent_loss_category() -> None:
    cohort = cohort_from_payload(
        _payload([_entry("1111", "selected", ploss="中"), _entry("2222", "rejected")])
    )

    assert cohort is not None
    assert [item.ticker for item in cohort.selected] == ["1111"]
    assert cohort.selected[0].ploss == "中"
    assert cohort.selected[0].catalyst_date == date(2026, 2, 14)
    assert [item.ticker for item in cohort.rejected] == ["2222"]


def test_selected_and_rejected_are_measured_against_the_pool_they_came_from() -> None:
    cohort = cohort_from_payload(
        _payload(
            [
                _entry("1111", "selected"),
                _entry("2222", "selected"),
                _entry("3333", "rejected"),
                _entry("4444", "rejected"),
            ]
        )
    )
    assert cohort is not None
    rows = [
        _forward("1111", 0.20),
        _forward("2222", 0.10),
        _forward("3333", 0.0),
        _forward("4444", -0.10),
    ]

    result = evaluate_cohort(cohort, rows, horizon="3m")

    # Pool median of 20 / 10 / 0 / -10 is 5%; the judgment beat it, the rejects did not.
    assert result["pool_median_return_pct"] == 5.0
    assert result["selected"]["median_excess_pct"] == 10.0
    assert result["rejected"]["median_excess_pct"] == -10.0


def test_machine_cohort_takes_the_same_number_of_names_the_judgment_took() -> None:
    cohort = cohort_from_payload(
        _payload(
            [
                _entry("1111", "selected"),
                _entry("2222", "rejected"),
                _entry("3333", "rejected"),
            ]
        )
    )
    assert cohort is not None
    # The machine would have taken 3333 alone; the judgment took 1111 alone.
    cohort = with_machine_estimates(cohort, {"1111": 0.05, "2222": 0.06, "3333": 0.09})
    rows = [_forward("1111", 0.20), _forward("2222", 0.0), _forward("3333", -0.10)]

    result = evaluate_cohort(cohort, rows, horizon="3m")

    assert result["machine_basis"] == "run_estimate"
    assert result["machine_top_n"]["n"] == 1
    assert result["machine_top_n"]["median_return_pct"] == -10.0
    assert result["selected"]["median_return_pct"] == 20.0


def test_machine_cohort_is_unresolved_when_the_bound_run_is_gone() -> None:
    # An older shortlist's run is pruned, and filling the estimate from a different
    # run would compare the judgment against numbers it never saw.
    cohort = cohort_from_payload(_payload([_entry("1111", "selected"), _entry("2222", "rejected")]))
    assert cohort is not None

    result = evaluate_cohort(cohort, [_forward("1111", 0.1), _forward("2222", 0.0)], horizon="3m")

    assert result["machine_basis"] == "unresolved_pruned_run"
    assert result["machine_top_n"]["n"] == 0


def test_a_horizon_that_has_not_matured_reports_unresolved_rather_than_zero() -> None:
    cohort = cohort_from_payload(_payload([_entry("1111", "selected"), _entry("2222", "rejected")]))
    assert cohort is not None

    result = evaluate_cohort(cohort, [_forward("1111", None), _forward("2222", None)], horizon="3m")

    assert result["status"] == "unresolved"
    assert result["pool_size"] == 2


def test_drawdown_is_reported_before_the_return_matures() -> None:
    # How far a name has fallen so far is already observable, and it is the quantity
    # the permanent-loss judgment was about.
    cohort = cohort_from_payload(
        _payload([_entry("1111", "selected", ploss="低"), _entry("2222", "selected", ploss="高")])
    )
    assert cohort is not None

    result = evaluate_cohort(
        cohort,
        [_forward("1111", None), _forward("2222", None)],
        horizon="3m",
        drawdowns={"1111": -0.02, "2222": -0.25},
        drawdown_window_end=date(2026, 2, 20),
    )

    assert result["status"] == "unresolved"
    assert result["drawdown_window_end"] == "2026-02-20"
    # Reported least-concerning first, so the scale reads down.
    assert [item["ploss"] for item in result["ploss"]] == ["低", "高"]
    assert result["ploss"][1]["worst_drawdown_pct"] == -25.0
