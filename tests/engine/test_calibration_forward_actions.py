"""Stored action rows use the same decoder in market reads and forward evaluation."""

from contextlib import closing
from datetime import date

import pytest
from tests.helpers.screening_sqlite import add_source_coverage

from baibai_engine.machine_analysis.historical_evaluation.forward import compute_forward_returns
from baibai_engine.market.jquants_decode import JQuantsProviderError
from baibai_engine.market.price_reader import read_adjustment_factor_bars
from baibai_engine.storage.observations.schema import open_connection


@pytest.mark.parametrize("close", [None, 100.0])
@pytest.mark.parametrize("consumer", ["market", "forward"])
def test_stored_action_without_factor_raises_contextual_source_error(tmp_path, close, consumer):
    path = tmp_path / "market.sqlite"
    day = date(2026, 10, 23)
    with closing(open_connection(path)) as connection, connection:
        connection.execute(
            "INSERT INTO jquants_daily_bars "
            "(ticker, traded_at, close, adjustment_factor, ex_right_type) "
            "VALUES ('1234', ?, ?, NULL, '3')",
            (day.isoformat(), close),
        )
        add_source_coverage(
            connection,
            source="jquants_daily_bars",
            coverage_key="fixture-actions",
            min_date=day.isoformat(),
            max_date=day.isoformat(),
            record_count=1,
        )
    if consumer == "market":
        with pytest.raises(JQuantsProviderError, match="1234 on 2026-10-23"):
            read_adjustment_factor_bars(path, day, day)
    else:
        with pytest.raises(JQuantsProviderError, match="1234 on 2026-10-23"):
            compute_forward_returns(
                path,
                asofs=(day,),
                tickers=("1234",),
                horizons=("1y",),
                control_event_exits={},
                failure_exits={},
            )
