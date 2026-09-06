"""Daily observations compare published method identities, not file names."""

from dataclasses import replace
from datetime import date, datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from baibai_web.readmodel.builders import build_daily_delta
from baibai_web.sources.types import ScreeningRunRecord


def _run(day: int) -> ScreeningRunRecord:
    return ScreeningRunRecord(
        run_id=f"run-{day}",
        as_of=date(2026, 9, day),
        generated_at=datetime(2026, 9, day, 18, tzinfo=ZoneInfo("Asia/Tokyo")),
        universe_size=3,
        run_revision_id=f"revision-{day}",
        rules_ref="rules.yaml",
        screening_rules_hash="screening-v1",
        er_model_version="er-v1",
        rows=(),
    )


def _pool(tickers: list[str], er: float, method_hash: str | None = "discovery-v1") -> list[dict]:
    return [
        {
            "payload": {
                "method": {"method_hash": method_hash},
                "entries": [
                    {
                        "ticker": ticker,
                        "name": ticker,
                        "analysis": {"expected_return": {"er_annual": er}},
                    }
                    for ticker in tickers
                ],
            }
        }
    ]


def _sources(latest=None, previous=None, current_pool=None, previous_pool=None):
    screening = Mock()
    screening.latest_run.return_value = latest or _run(4)
    screening.previous_run.return_value = previous or _run(3)
    screening.review_sets.side_effect = [
        _pool(["1003", "1002", "1001"], 0.2) if current_pool is None else current_pool,
        _pool(["1004", "1001"], 0.1) if previous_pool is None else previous_pool,
    ]
    ledger = Mock()
    ledger.exists.return_value = False
    market = Mock()
    market.exists.return_value = True
    market.disclosures_after.return_value = []
    return screening, ledger, Mock(), market


@pytest.mark.parametrize("change", ["none", "path", "screening", "discovery", "er"])
def test_daily_delta_compares_method_identity_by_output(change: str) -> None:
    latest = _run(4)
    if change == "path":
        latest = replace(latest, rules_ref="renamed.yaml")
    elif change == "screening":
        latest = replace(latest, screening_rules_hash="screening-v2")
    elif change == "er":
        latest = replace(latest, er_model_version="er-v2")
    current = _pool(
        ["1003", "1002", "1001"], 0.2, "discovery-v2" if change == "discovery" else "discovery-v1"
    )
    sources = _sources(latest=latest, current_pool=current)
    view = build_daily_delta(*sources)
    changed = change in {"screening", "discovery"}
    assert view.method_changed is changed
    assert [row.ticker for row in view.entered] == ([] if changed else ["1002", "1003"])
    assert [row.ticker for row in view.exited] == ([] if changed else ["1004"])
    assert [row.ticker for row in view.er_moves] == ([] if changed or change == "er" else ["1001"])
    assert view.er_moves_total == (0 if changed or change == "er" else 1)
    assert ("review_set_estimate" in view.unavailable) is (change == "er")
    assert [call.kwargs["run_revision_id"] for call in sources[0].review_sets.call_args_list] == [
        "revision-4",
        "revision-3",
    ]


@pytest.mark.parametrize("side", ["latest", "previous"])
@pytest.mark.parametrize("identity", ["screening", "discovery", "er"])
def test_missing_identity_is_not_treated_as_matching(side: str, identity: str) -> None:
    latest, previous = _run(4), _run(3)
    current, earlier = _pool(["1002", "1001"], 0.2), _pool(["1001"], 0.1)
    if identity == "discovery":
        (current if side == "latest" else earlier)[0]["payload"]["method"] = {}
    else:
        field = "screening_rules_hash" if identity == "screening" else "er_model_version"
        if side == "latest":
            latest = replace(latest, **{field: None})
        else:
            previous = replace(previous, **{field: None})
    view = build_daily_delta(*_sources(latest, previous, current, earlier))
    assert not view.method_changed
    assert view.er_moves == []
    assert view.er_moves_total == 0
    assert [item.ticker for item in view.entered] == (["1002"] if identity == "er" else [])
    assert ("review_set_estimate" if identity == "er" else "review_set") in view.unavailable


@pytest.mark.parametrize("missing", ["latest", "previous", "review_set", "empty"])
def test_absent_comparison_and_empty_pool_are_distinct(missing: str) -> None:
    sources = _sources(current_pool=_pool([], 0.2), previous_pool=_pool([], 0.1))
    if missing == "latest":
        sources[0].latest_run.return_value = None
    elif missing == "previous":
        sources[0].previous_run.return_value = None
    elif missing == "review_set":
        sources[0].review_sets.side_effect = [[], _pool([], 0.1)]
    view = build_daily_delta(*sources)
    expected = {
        "latest": "screening_run",
        "previous": "previous_screening_run",
        "review_set": "review_set",
    }.get(missing)
    assert view.entered == view.exited == view.er_moves == []
    assert not view.method_changed
    if expected:
        assert expected in view.unavailable
    else:
        assert view.unavailable == ["holdings"]


def test_method_change_does_not_skip_holdings(mocker) -> None:
    sources = _sources(latest=replace(_run(4), screening_rules_hash="changed"))
    sources[1].exists.return_value = True
    sources[2].load_errors.return_value = []
    holding_delta = mocker.patch(
        "baibai_web.readmodel.builders._holding_deltas", return_value=([], 2, 3)
    )
    view = build_daily_delta(*sources)
    assert view.method_changed
    assert (view.holdings_without_fair_value, view.holdings_without_price) == (2, 3)
    holding_delta.assert_called_once()


@pytest.mark.parametrize("side", ["current", "previous", "both"])
@pytest.mark.parametrize("shape", ["null", "absent"])
@pytest.mark.parametrize("comparable", [False, True])
def test_partial_estimate_missing_is_visible_without_hiding_comparable_movers(
    side: str, shape: str, comparable: bool
) -> None:
    tickers = ["1001", "1002"] if comparable else ["1001"]
    current, previous = _pool(tickers, 0.2), _pool(tickers, 0.1)
    for name, publications in [("current", current), ("previous", previous)]:
        if side not in {name, "both"}:
            continue
        expected = publications[0]["payload"]["entries"][0]["analysis"]["expected_return"]
        if shape == "null":
            expected["er_annual"] = None
        else:
            del expected["er_annual"]
    view = build_daily_delta(*_sources(current_pool=current, previous_pool=previous))
    assert "review_set_estimate" in view.unavailable
    assert [row.ticker for row in view.er_moves] == (["1002"] if comparable else [])
    assert view.er_moves_total == int(comparable)
    assert view.entered == view.exited == []


def test_estimate_missing_outside_shared_tickers_does_not_prevent_comparison() -> None:
    current, previous = _pool(["1001", "1002"], 0.2), _pool(["1001", "1003"], 0.1)
    for publications in [current, previous]:
        publications[0]["payload"]["entries"][1]["analysis"]["expected_return"]["er_annual"] = None
    view = build_daily_delta(*_sources(current_pool=current, previous_pool=previous))
    assert "review_set_estimate" not in view.unavailable
    assert [row.ticker for row in view.entered] == ["1002"]
    assert [row.ticker for row in view.exited] == ["1003"]
    assert [row.ticker for row in view.er_moves] == ["1001"]


def test_disjoint_review_sets_have_no_unmeasured_shared_estimates() -> None:
    current, previous = _pool(["1001"], 0.2), _pool(["1002"], 0.1)
    for publications in [current, previous]:
        publications[0]["payload"]["entries"][0]["analysis"]["expected_return"]["er_annual"] = None
    view = build_daily_delta(*_sources(current_pool=current, previous_pool=previous))
    assert "review_set_estimate" not in view.unavailable
    assert view.er_moves_total == 0
    assert [row.ticker for row in view.entered] == ["1001"]
    assert [row.ticker for row in view.exited] == ["1002"]


def test_mover_total_retains_rows_beyond_the_display_cap() -> None:
    tickers = [str(1000 + index) for index in range(20)]
    view = build_daily_delta(
        *_sources(
            current_pool=_pool(tickers, 0.2),
            previous_pool=_pool(tickers, 0.1),
        )
    )
    assert view.er_moves_total == 20
    assert len(view.er_moves) == 5
