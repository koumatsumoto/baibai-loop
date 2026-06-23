from __future__ import annotations

from datetime import date
from pathlib import Path

from baibai_loop.screening.forward.forward_return import HorizonAggregate
from baibai_loop.screening.forward.screening_replay import (
    ProfileWeekResult,
    ReplayResult,
    replay_to_payload,
)
from baibai_loop.screening.forward.weeks import WeekSpec, discover_week_specs


def _write_week(root: Path, asof: date) -> None:
    path = root / f"{asof:%Y}" / f"{asof:%m}" / f"{asof:%Y-%m-%d}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("candidates: []\n", encoding="utf-8")


def test_discover_week_specs_sorts_and_flags_holdout(tmp_path: Path) -> None:
    for asof in (date(2026, 5, 8), date(2026, 4, 24), date(2026, 5, 1)):
        _write_week(tmp_path, asof)
    specs = discover_week_specs(tmp_path, holdout_weeks=1)
    assert [spec.asof for spec in specs] == [
        date(2026, 4, 24),
        date(2026, 5, 1),
        date(2026, 5, 8),
    ]
    assert [spec.is_holdout for spec in specs] == [False, False, True]


def test_discover_week_specs_ignores_non_date_files(tmp_path: Path) -> None:
    _write_week(tmp_path, date(2026, 5, 1))
    (tmp_path / "2026" / "05" / "notes.yaml").write_text("x: 1\n", encoding="utf-8")
    specs = discover_week_specs(tmp_path)
    assert [spec.asof for spec in specs] == [date(2026, 5, 1)]


def test_replay_to_payload_serializes_weeks() -> None:
    result = ReplayResult(
        profiles=("balanced",),
        horizon_weeks=(1, 4),
        eval_cap=date(2026, 6, 5),
        benchmark_ticker="1321",
        regime_lens=True,
        results=(
            ProfileWeekResult(
                week=date(2026, 5, 1),
                profile="balanced",
                is_holdout=False,
                market_regime={"regime": "risk_on_rally"},
                recommended_tickers=("9682", "9692"),
                recommended=(),
                fast_dislocation_count=37,
                long_hold_counts={"high": 1},
                suppressed_count=4,
                distributions={"selection_playbook": {"sales-discount-growth": 2}},
                forward_returns=(),
                forward_aggregates=(
                    HorizonAggregate(
                        weeks=1, count=2, mean_return=0.01, median_return=0.01, mean_relative=-0.02
                    ),
                ),
            ),
        ),
    )
    payload = replay_to_payload(result)
    assert payload["eval_cap"] == "2026-06-05"
    assert payload["weeks"][0]["recommended_tickers"] == ["9682", "9692"]
    assert payload["weeks"][0]["forward_aggregates"][0]["count"] == 2
    assert payload["weeks"][0]["distributions"]["selection_playbook"] == {
        "sales-discount-growth": 2
    }
    assert payload["regime_lens"] is True
    assert payload["weeks"][0]["market_regime"] == {"regime": "risk_on_rally"}


def test_week_spec_defaults_not_holdout() -> None:
    spec = WeekSpec(asof=date(2026, 5, 1), candidates_path=Path("x.yaml"))
    assert spec.is_holdout is False
