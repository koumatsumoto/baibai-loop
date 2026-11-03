"""日次バーの途中値を削除せず、受領時点のsession状態をL2へ渡す。"""

from contextlib import closing
from datetime import UTC, date, datetime

import pytest

from baibai_engine.data_collection.receipts.native_sources.records import store_snapshot
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.machine_analysis.generation.runners.reading_rules import (
    DEFAULT_RULES_PATH,
    load_reading_rules,
)
from baibai_engine.machine_analysis.models.market_context.reading.compute import compute_reading
from baibai_engine.machine_analysis.models.market_context.reading.models import snapshot_payload
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
    load_definitions,
)
from baibai_engine.machine_analysis.observations.economic_indicators.derived import derive_series
from baibai_engine.machine_analysis.observations.economic_indicators.native import (
    _yahoo_bar_context,
    read_native_series,
)

DAY = date(2026, 11, 2)
START = datetime(2026, 11, 2, 14, 30, tzinfo=UTC)
END = datetime(2026, 11, 2, 21, tzinfo=UTC)
RECEIVED = datetime(2026, 11, 2, 17, 27, tzinfo=UTC)


def row(symbol="^SOX", value=100):
    return {
        "timestamp": START.timestamp(),
        "metadata": {
            "meta": {
                "symbol": symbol,
                "currentTradingPeriod": {
                    "regular": {"start": START.timestamp(), "end": END.timestamp()}
                },
            }
        },
        "indicators": {"quote": [{"close": value}]},
    }


@pytest.mark.parametrize(
    ("receipt", "bar", "flag"),
    [
        (RECEIVED, START, True),
        (END, START, False),
        (datetime(2026, 11, 3, tzinfo=UTC), START, False),
        (RECEIVED, datetime(2026, 10, 30, 13, 30, tzinfo=UTC), False),
    ],
)
def test_only_matching_session_and_retained_receipt_establish_intraday(receipt, bar, flag):
    record = row()
    record["timestamp"] = bar.timestamp()
    flags, notes = _yahoo_bar_context([record], receipt)
    assert bool(flags) == flag
    assert "not a certification" in notes[0]
    if flag:
        assert flags == ("intraday_bar_at_receipt",)
        assert END.isoformat() in notes[1]
        assert receipt.isoformat() in notes[1]


@pytest.mark.parametrize(
    "clocks",
    [
        None,
        {},
        {"start": True, "end": END.timestamp()},
        {"start": END.timestamp(), "end": START.timestamp()},
        {"start": 1e100, "end": 1e101},
    ],
)
def test_unknown_clocks_do_not_certify_finality_or_drop_value(clocks):
    record = row()
    record["metadata"]["meta"]["currentTradingPeriod"] = {"regular": clocks}
    flags, notes = _yahoo_bar_context([record], RECEIVED)
    assert not flags
    assert "unverified" in notes[1]


def test_retained_native_values_and_reading_and_derived_notes(tmp_path):
    registry = load_definitions().by_id()
    path = tmp_path / "economic.sqlite"
    with closing(initialize_database(path)) as c:
        for key, value in [("gold", 4000), ("copper", 6)]:
            definition = registry[key]
            store_snapshot(
                c,
                source_id=definition.source_id,
                observed_at=DAY,
                fetched_at=RECEIVED,
                records=[row(definition.reading_key, value)],
            )
        c.commit()
        before = path.read_bytes()
        inputs = {
            key: read_native_series(c, registry[key], start=DAY, end=DAY)
            for key in ["gold", "copper"]
        }
        assert inputs["gold"][0].value == 4000
        assert inputs["copper"][0].value == 6
        assert inputs["gold"][0].vintage_at == RECEIVED
        assert inputs["gold"][0].quality_flags == ("intraday_bar_at_receipt",)
        derived = derive_series(registry["gold_copper_ratio"], inputs, start=DAY, end=DAY)
        assert derived[0].value == 4000 / 6
        assert any(n == "input[gold] flag: intraday_bar_at_receipt" for n in derived[0].notes)
        inputs["gold_copper_ratio"] = derived
        snapshot = compute_reading(
            series=[registry[k] for k in inputs],
            reader=lambda key, start, end: inputs[key],
            rules=load_reading_rules(DEFAULT_RULES_PATH),
            rules_revision="test",
            asof=DAY,
        )
        payload = {s["series_id"]: s for s in snapshot_payload(snapshot)["series"]}
        assert "intraday_bar_at_receipt" in payload["gold"]["flags"]
        assert any("intraday_bar_at_receipt" in n for n in payload["gold_copper_ratio"]["notes"])
        assert payload["gold"]["latest_value"] == 4000
        assert path.read_bytes() == before
