from __future__ import annotations

from baibai_loop.validate.registry import evaluate_kill_switch, has_validator_callable


def test_registry_has_kill_switch_implementation() -> None:
    assert has_validator_callable("boj_eve_window")
    assert not has_validator_callable("unknown_window")


def test_boj_eve_window_evaluates_events() -> None:
    assert (
        evaluate_kill_switch(
            "boj_eve_window",
            {"at": "2026-05-05T20:00:00+09:00", "window_days": 1},
            [{"event_id": "boj-20260506", "date": "2026-05-06", "kind": "boj"}],
        )
        is True
    )


def test_earnings_window_matches_same_ticker_only() -> None:
    assert (
        evaluate_kill_switch(
            "earnings_straddle_window",
            {"ticker": "9682", "at": "2026-05-05T20:00:00+09:00", "window_days": 1},
            [
                {"event_id": "9692-earnings-20260506", "date": "2026-05-06", "kind": "earnings"}
            ],
        )
        is False
    )
