from __future__ import annotations

from datetime import date, timedelta

from baibai_engine.market.bars import JQuantsDailyBar
from baibai_engine.screening.metrics.history import _avg_daily_volume


def test_the_average_volume_window_is_twenty_sessions() -> None:
    """The window length is a number the margin figures are divided by.

    Days of trading is a margin balance over this average, so a window one session
    wider moves every overhang figure without any read failing. The counts here are
    written out rather than derived from `AVG_VOLUME_SESSIONS`: a fixture built from
    the constant moves with it and can never say where the edge is.
    """

    asof = date(2026, 7, 10)
    # Twenty-one sessions, the oldest carrying a volume nothing else comes near.
    bars = [
        JQuantsDailyBar(
            ticker="130A",
            traded_at=asof - timedelta(days=20 - offset),
            open=None,
            high=None,
            low=None,
            close=100.0,
            volume=(1_000_000.0 if offset == 0 else 1_000.0),
            turnover_value=None,
            adjustment_factor=1.0,
        )
        for offset in range(21)
    ]

    # Twenty sessions back from the newest bar leaves the outlier one session outside.
    assert _avg_daily_volume(bars, asof) == 1_000.0
    # A window that reached one session further would take it in, which is what makes
    # this the edge rather than a restatement of the constant.
    assert _avg_daily_volume(bars, asof, sessions=21) != 1_000.0


def test_the_average_volume_needs_fifteen_of_the_twenty_sessions_to_report() -> None:
    """The floor decides whether an illiquid name gets a days-of-volume figure at all.

    Days of trading is a margin balance over this average, and the margin overhang axis
    is where thin names matter most — demanding all twenty would drop exactly them,
    while accepting two would put a figure on a name that barely trades. The counts are
    written out rather than derived from the constants: a fixture built from them moves
    with them and can never say where the floor is.
    """

    asof = date(2026, 7, 10)

    def _bars(reported: int) -> list[JQuantsDailyBar]:
        # Twenty sessions, of which `reported` carry a volume and the rest carry none.
        return [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=asof - timedelta(days=19 - offset),
                open=None,
                high=None,
                low=None,
                close=100.0,
                volume=(1_000.0 if offset < reported else None),
                turnover_value=None,
                adjustment_factor=1.0,
            )
            for offset in range(20)
        ]

    assert _avg_daily_volume(_bars(15), asof) == 1_000.0
    assert _avg_daily_volume(_bars(14), asof) is None

    # The other half of the rule: the twenty sessions have to exist at all. A name
    # listed last week reports every day it has traded, so the reporting floor alone
    # would hand it a days-of-volume figure off a handful of sessions.
    assert _avg_daily_volume(_bars(19)[1:], asof) is None
