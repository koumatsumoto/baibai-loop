from __future__ import annotations

from tools.measure_exploration_lane import (
    CandidateObservation,
    CohortPair,
    compute_effect,
    resolve_verdict,
)

from baibai_engine.screening.selection.exploration import (
    ExplorationCandidate,
    select_exploration,
)


def _row(
    ticker: str,
    *,
    rank: int | None = None,
    metric: float | None = None,
    in_population: bool = True,
) -> ExplorationCandidate:
    return ExplorationCandidate(
        ticker=ticker,
        full_rank=rank,
        in_population=in_population,
        normalized_per_3fy=metric,
    )


def _population(count: int, *, start: float = 100.0) -> list[ExplorationCandidate]:
    """Unranked liquidity rows that only set where the good-decile cutoff lands."""
    return [_row(f"P{index:04d}", metric=start + index) for index in range(count)]


def _band(metrics: dict[int, float | None]) -> list[ExplorationCandidate]:
    return [_row(f"B{rank}", rank=rank, metric=metric) for rank, metric in metrics.items()]


def test_qualified_band_name_with_the_best_rank_wins_over_the_cheapest_multiple() -> None:
    # Ranks 21 and 30 both sit in the good decile; 30 is far cheaper on the axis.
    # The policy must still return 21, because ordering belongs to the calibrated
    # E[r] rank and the metric only decides eligibility.
    rows = [
        *_population(90),
        *_band(dict.fromkeys(range(22, 30), 500.0)),
        _row("B21", rank=21, metric=1.0),
        _row("B30", rank=30, metric=0.5),
    ]
    result = select_exploration(rows)
    assert result.status == "available"
    assert result.ticker == "B21"
    assert result.source_rank == 21


def test_ties_on_rank_are_broken_by_ticker_so_a_panel_yields_one_answer() -> None:
    rows = [
        *_population(90),
        *_band(dict.fromkeys(range(23, 33), 500.0)),
        _row("BBB", rank=22, metric=1.0),
        _row("AAA", rank=22, metric=1.0),
    ]
    assert select_exploration(rows).ticker == "AAA"


def test_band_with_fewer_than_ten_metric_rows_abstains() -> None:
    rows = [
        *_population(90),
        *_band(dict.fromkeys(range(21, 30), 1.0)),
        *_band(dict.fromkeys(range(30, 41))),
    ]
    result = select_exploration(rows)
    assert result.status == "insufficient_metric_coverage"
    assert result.ticker is None
    assert result.band_metric_count == 9


def test_band_with_no_good_decile_member_yields_no_candidate() -> None:
    rows = [
        *_population(90),
        *_band(dict.fromkeys(range(21, 41), 500.0)),
    ]
    result = select_exploration(rows)
    assert result.status == "no_qualified_candidate"
    assert result.ticker is None
    assert result.band_metric_count == 20


def test_a_different_longlist_depth_moves_the_band_so_the_policy_declines() -> None:
    rows = [*_population(90), *_band(dict.fromkeys(range(21, 41), 1.0))]
    result = select_exploration(rows, longlist_top=10)
    assert result.status == "unsupported_longlist_top"
    assert result.ticker is None


def test_non_positive_and_missing_multiples_never_count_as_cheap() -> None:
    # A negative multiple sorts below every positive one. Reading it as the cheap
    # end would qualify exactly the names whose three-year profit was not positive.
    rows = [
        *_population(90),
        _row("NEG", rank=21, metric=-4.0),
        _row("NUL", rank=22, metric=None),
        *_band(dict.fromkeys(range(23, 33), 1.0)),
    ]
    result = select_exploration(rows)
    assert result.ticker == "B23"
    assert result.band_metric_count == 10
    assert result.decile_population_size == 100


def test_unranked_liquidity_rows_still_set_the_decile_cutoff() -> None:
    # Rows without an E[r] carry no rank but belong to the population the axis was
    # measured on. Dropping them would let the cutoff drift with how many names
    # happened to carry an estimate that month.
    band = _band(dict.fromkeys(range(21, 41), 50.0))
    without = select_exploration([*_population(9, start=100.0), *band])
    with_cheaper = select_exploration(
        [*_population(9, start=100.0), *_population(100, start=1.0), *band]
    )
    assert without.ticker == "B21"
    assert with_cheaper.status == "no_qualified_candidate"


def test_rows_outside_the_liquidity_population_do_not_move_the_cutoff() -> None:
    rows = [
        *_population(90),
        *[_row(f"X{index}", metric=0.1, in_population=False) for index in range(50)],
        *_band(dict.fromkeys(range(22, 41), 500.0)),
        _row("B21", rank=21, metric=1.0),
    ]
    result = select_exploration(rows)
    # 90 liquidity rows plus the 20 ranked band rows; the 50 cheap rows outside the
    # population would otherwise fill the decile and push B21 out of it.
    assert result.decile_population_size == 110
    assert result.ticker == "B21"


def _observation(
    ticker: str, excess: float | None, *, imputable: bool = False
) -> CandidateObservation:
    return CandidateObservation(
        ticker=ticker,
        excess=excess,
        imputable=imputable,
        status="resolved" if excess is not None else "unresolved_missing_exit",
    )


def _pair(
    asof: str,
    *,
    exploration: tuple[str, float | None],
    comparator: tuple[str, float | None],
    imputable: bool = False,
    population_median: float = 0.0,
) -> CohortPair:
    return CohortPair(
        asof=asof,
        horizon="3y",
        basis="price",
        exploration=_observation(*exploration, imputable=imputable),
        comparator=_observation(*comparator),
        population_n=200,
        population_median=population_median,
        same_ticker=exploration[0] == comparator[0],
    )


def test_a_month_where_both_lanes_name_the_same_ticker_counts_as_a_zero() -> None:
    pairs = [
        _pair("2020-01-31", exploration=("A", 0.5), comparator=("B", 0.1)),
        _pair("2020-02-28", exploration=("C", 0.2), comparator=("C", 0.2)),
        _pair("2020-03-31", exploration=("D", 0.2), comparator=("D", 0.2)),
    ]
    effect = compute_effect(pairs, imputation="reported", weighting="cohort")
    assert effect.n == 3
    assert effect.changed_n == 1
    assert effect.pair_delta_median == 0.0
    assert effect.positive_share == 1 / 3
    assert effect.changed_only_median == 0.4


def test_reported_case_drops_a_pair_that_both_imputations_keep() -> None:
    pairs = [
        _pair("2020-01-31", exploration=("A", 0.1), comparator=("B", 0.0)),
        _pair(
            "2020-02-28",
            exploration=("C", None),
            comparator=("D", 0.0),
            imputable=True,
            population_median=0.2,
        ),
    ]
    reported = compute_effect(pairs, imputation="reported", weighting="cohort")
    total_loss = compute_effect(pairs, imputation="total_loss", weighting="cohort")
    neutral = compute_effect(pairs, imputation="neutral", weighting="cohort")
    assert reported.n == 1
    assert total_loss.n == 2
    assert neutral.n == 2
    # A name with no exit value is worth either nothing or what the cohort made.
    assert total_loss.exploration_trap_rate == 0.5
    assert neutral.exploration_trap_rate == 0.0


def test_an_unresolved_pair_that_is_not_imputable_stays_out_of_every_case() -> None:
    pairs = [
        _pair("2020-01-31", exploration=("A", 0.1), comparator=("B", 0.0)),
        _pair("2020-02-28", exploration=("C", None), comparator=("D", 0.0), imputable=False),
    ]
    for imputation in ("reported", "total_loss", "neutral"):
        assert compute_effect(pairs, imputation=imputation, weighting="cohort").n == 1


def test_ticker_weighting_gives_a_repeated_name_one_vote() -> None:
    pairs = [
        _pair("2020-01-31", exploration=("A", 0.9), comparator=("X", 0.0)),
        _pair("2020-02-28", exploration=("A", 0.7), comparator=("Y", 0.0)),
        _pair("2020-03-31", exploration=("A", 0.5), comparator=("Z", 0.0)),
        _pair("2020-04-30", exploration=("B", -0.4), comparator=("W", 0.0)),
    ]
    cohort = compute_effect(pairs, imputation="reported", weighting="cohort")
    ticker = compute_effect(pairs, imputation="reported", weighting="ticker")
    assert cohort.n == 4
    assert cohort.positive_share == 0.75
    assert ticker.n == 2
    assert ticker.positive_share == 0.5
    assert ticker.pair_delta_median == (0.7 - 0.4) / 2


def test_ticker_weighting_never_reports_more_changed_units_than_units() -> None:
    pairs = [
        _pair("2020-01-31", exploration=("A", 0.9), comparator=("X", 0.0)),
        _pair("2020-02-28", exploration=("A", 0.7), comparator=("Y", 0.0)),
        _pair("2020-03-31", exploration=("B", 0.2), comparator=("B", 0.2)),
    ]
    effect = compute_effect(pairs, imputation="reported", weighting="ticker")
    assert effect.changed_n <= effect.n


class _Window:
    def __init__(self, *, sufficient: bool, cells: dict[str, dict[str, object]]) -> None:
        self.sufficient = sufficient
        self.effects = cells


def _cell(delta: float, *, passes: bool) -> dict[str, object]:
    return {"pair_delta_median": delta, "pass": passes}


def test_a_coverage_shortfall_outranks_every_effect_conclusion() -> None:
    windows = {
        "a": _Window(sufficient=False, cells={"price": _cell(0.5, passes=True)}),
        "b": _Window(sufficient=True, cells={"price": _cell(0.5, passes=True)}),
    }
    verdict, reasons = resolve_verdict(windows)  # type: ignore[arg-type]
    assert verdict == "insufficient"
    assert reasons == ["insufficient:a"]


def test_a_sign_that_splits_across_cells_is_inconclusive_not_negative() -> None:
    windows = {
        "a": _Window(sufficient=True, cells={"price": _cell(0.5, passes=True)}),
        "b": _Window(sufficient=True, cells={"price": _cell(-0.5, passes=False)}),
    }
    verdict, _ = resolve_verdict(windows)  # type: ignore[arg-type]
    assert verdict == "inconclusive"


def test_one_agreed_sign_with_a_failing_predicate_is_negative() -> None:
    windows = {
        "a": _Window(sufficient=True, cells={"price": _cell(0.5, passes=True)}),
        "b": _Window(sufficient=True, cells={"price": _cell(0.01, passes=False)}),
    }
    verdict, reasons = resolve_verdict(windows)  # type: ignore[arg-type]
    assert verdict == "negative"
    assert reasons == ["effect_predicate_failed:b/price"]


def test_every_cell_passing_is_the_only_route_to_adoption() -> None:
    windows = {
        "a": _Window(sufficient=True, cells={"price": _cell(0.5, passes=True)}),
        "b": _Window(sufficient=True, cells={"total": _cell(0.4, passes=True)}),
    }
    verdict, reasons = resolve_verdict(windows)  # type: ignore[arg-type]
    assert verdict == "adoption_candidate"
    assert reasons == []
