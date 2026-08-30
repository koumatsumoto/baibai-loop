from __future__ import annotations

import contextlib
import io
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_engine.screening.calibration.forward import ForwardReturnRow
from baibai_engine.screening.cli.shortlist_outcome_cli import shortlist_outcome_command
from baibai_engine.screening.shortlist_outcome import (
    cohort_from_payload,
    evaluate_cohort,
    evaluate_machine_counterfactual,
    with_machine_estimates,
)
from baibai_engine.screening.sqlite_cache import open_connection

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

    assert result["machine_basis"] == "judgment_estimate"
    assert result["machine_top_n"]["n"] == 1
    assert result["machine_top_n"]["median_return_pct"] == -10.0
    assert result["selected"]["median_return_pct"] == 20.0


def test_machine_basis_separates_a_missing_estimate_from_a_cycle_that_selected_nothing() -> None:
    # A cycle that selected nothing is a normal outcome; a judgment whose estimates
    # are gone is a measurement gap. Reporting both as one cause sends the reader to
    # the wrong place.
    no_estimate = cohort_from_payload(
        _payload([_entry("1111", "selected"), _entry("2222", "rejected")])
    )
    no_selection = cohort_from_payload(_payload([_entry("2222", "rejected")]))
    assert no_estimate is not None
    assert no_selection is not None
    rows = [_forward("1111", 0.1), _forward("2222", 0.0)]

    assert evaluate_cohort(no_estimate, rows, horizon="3m")["machine_basis"] == "estimate_missing"
    assert evaluate_cohort(no_selection, rows, horizon="3m")["machine_basis"] == "no_selection"


def test_fixed_machine_counterfactual_compares_top_one_with_cash_and_pool() -> None:
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
    cohort = with_machine_estimates(cohort, {"1111": 0.05, "2222": 0.06, "3333": 0.09})

    result = evaluate_machine_counterfactual(
        cohort,
        [_forward("1111", 0.1), _forward("2222", 0.0), _forward("3333", 0.2)],
        horizon="3m",
        top_n=1,
    )

    assert result["status"] == "resolved"
    assert result["target_date"] == "2026-04-30"
    assert result["pool_median_return_pct"] == 10.0
    assert result["machine_top_n"] == {
        "n": 1,
        "tickers": ["3333"],
        "resolved": 1,
        "unresolved": 0,
        "median_return_pct": 20.0,
        "median_excess_vs_cash_pct": 20.0,
        "median_excess_vs_pool_pct": 10.0,
    }


def test_machine_counterfactual_does_not_rank_an_incomplete_estimate_set() -> None:
    cohort = cohort_from_payload(_payload([_entry("1111", "selected"), _entry("2222", "rejected")]))
    assert cohort is not None
    cohort = with_machine_estimates(cohort, {"1111": 0.10})

    result = evaluate_machine_counterfactual(
        cohort,
        [_forward("1111", 0.1), _forward("2222", 0.0)],
        horizon="3m",
        top_n=1,
    )

    assert result == {
        "horizon": "3m",
        "target_date": "2026-04-30",
        "status": "estimate_missing",
        "top_n": 1,
        "pool_size": 2,
        "estimate_count": 1,
    }


def test_a_cohort_with_names_but_no_prices_keeps_its_size_and_counts_the_gap() -> None:
    # Collapsing n to zero would report eight unpriced names the same as an empty
    # cohort, which is the coverage gap that has to be counted rather than hidden.
    cohort = cohort_from_payload(
        _payload(
            [_entry("1111", "selected"), _entry("2222", "rejected"), _entry("3333", "rejected")]
        )
    )
    assert cohort is not None

    result = evaluate_cohort(
        cohort,
        [_forward("1111", None), _forward("2222", 0.0), _forward("3333", 0.1)],
        horizon="3m",
    )

    assert result["selected"] == {
        "n": 1,
        "resolved": 0,
        "unresolved": 1,
        "median_return_pct": None,
        "median_excess_pct": None,
    }
    assert result["unresolved_count"] == 1
    assert result["unresolved_reason_counts"] == {"unresolved_future_horizon": 1}


def test_a_name_that_left_the_market_is_counted_rather_than_dropped_silently() -> None:
    # A premium buyout is a good outcome that happens to selected names; letting it
    # vanish from both the cohort and the benchmark pulls the judgment down unseen.
    cohort = cohort_from_payload(_payload([_entry("1111", "selected"), _entry("2222", "rejected")]))
    assert cohort is not None
    exited = ForwardReturnRow(
        asof=AS_OF.isoformat(),
        ticker="1111",
        horizon="3m",
        target_date="2026-04-30",
        resolved=False,
        price_return=None,
        stale_price=True,
        entry_date=AS_OF.isoformat(),
        exit_date="2026-02-27",
        status="unresolved_stale_exit",
    )

    result = evaluate_cohort(cohort, [exited, _forward("2222", 0.0)], horizon="3m")

    assert result["unpriced_exit_count"] == 1
    assert result["selected"]["unresolved"] == 1


def test_a_horizon_that_has_not_matured_reports_unresolved_rather_than_zero() -> None:
    cohort = cohort_from_payload(_payload([_entry("1111", "selected"), _entry("2222", "rejected")]))
    assert cohort is not None

    result = evaluate_cohort(cohort, [_forward("1111", None), _forward("2222", None)], horizon="3m")

    assert result["status"] == "unresolved"
    assert result["pool_size"] == 2
    # The coverage counts are reported on the unresolved branch too.
    assert result["unresolved_count"] == 2


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


def test_the_command_refuses_a_market_store_it_cannot_read(tmp_path: Path) -> None:
    # Every price the comparison rests on comes from that store. A store the readers
    # cannot open yields no bar date, which drops the drawdown block out of the
    # payload -- a cohort that never fell and a cohort nobody measured then read the
    # same. The refusal comes before any shortlist is read, so a missing app DB is
    # not what stops it.
    market = tmp_path / "market.sqlite"
    conn = open_connection(market)
    try:
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()
    output = tmp_path / "outcome.yaml"
    errors = io.StringIO()

    with contextlib.redirect_stderr(errors):
        code = shortlist_outcome_command(
            db_path=tmp_path / "baibai.sqlite",
            runs_db_path=None,
            sqlite_path=market,
            output_path=output,
        )

    assert code == 1
    assert "user_version 1" in errors.getvalue()
    assert not output.exists()


def test_the_command_refuses_a_market_store_that_is_absent(tmp_path: Path) -> None:
    output = tmp_path / "outcome.yaml"
    errors = io.StringIO()

    with contextlib.redirect_stderr(errors):
        code = shortlist_outcome_command(
            db_path=tmp_path / "baibai.sqlite",
            runs_db_path=None,
            sqlite_path=tmp_path / "absent.sqlite",
            output_path=output,
        )

    assert code == 1
    assert "not found" in errors.getvalue()
    assert not output.exists()


def test_the_catalyst_split_stays_inside_the_selected_cohort() -> None:
    """Rejected names carry no narrative, so a pool-wide split would report selection."""
    entries = [
        _entry("1111", "selected"),
        _entry("2222", "selected"),
        _entry("3333", "rejected"),
    ]
    # The second selection was made without a dated catalyst.
    narrative = entries[1]["narrative"]
    assert isinstance(narrative, dict)
    narrative["catalyst_date"] = None
    cohort = cohort_from_payload(_payload(entries))
    assert cohort is not None
    rows = [_forward("1111", 0.20), _forward("2222", -0.10), _forward("3333", 0.0)]

    result = evaluate_cohort(cohort, rows, horizon="3m")

    split = result["selected_by_catalyst"]
    assert split["basis"] == "selected_only"
    assert split["dated_catalyst"]["n"] == 1
    assert split["dated_catalyst"]["median_return_pct"] == 20.0
    assert split["no_dated_catalyst"]["n"] == 1
    assert split["no_dated_catalyst"]["median_return_pct"] == -10.0


def test_the_catalyst_split_reports_its_population_before_the_horizon_matures() -> None:
    cohort = cohort_from_payload(_payload([_entry("1111", "selected"), _entry("2222", "rejected")]))
    assert cohort is not None

    result = evaluate_cohort(cohort, [_forward("1111", None), _forward("2222", None)], horizon="3m")

    assert result["status"] == "unresolved"
    split = result["selected_by_catalyst"]
    assert split["dated_catalyst"]["n"] == 1
    assert split["dated_catalyst"]["resolved"] == 0
    assert split["dated_catalyst"]["median_return_pct"] is None
