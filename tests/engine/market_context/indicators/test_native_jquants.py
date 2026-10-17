from __future__ import annotations

from contextlib import closing
from datetime import UTC, date, datetime
from unittest.mock import Mock

import pytest

from baibai_engine.collection import economic_store as db
from baibai_engine.collection.economic_providers import jquants_flows, jquants_indices
from baibai_engine.collection.economic_providers.base import ProviderFetchStoppedError
from baibai_engine.collection.economic_records import read_snapshots, store_snapshot
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.reading.reader import build_store_observation_reader
from baibai_engine.read_api.freshness import indicator_latest_observed_at

PERIOD = date(2024, 8, 23)
FIRST = date(2024, 8, 29)
REVISED = date(2024, 9, 10)
FETCHED = datetime(2026, 10, 16, tzinfo=UTC)


def flow(published, value):
    return {
        "Section": "TSEPrime",
        "PubDate": published.isoformat(),
        "StDate": "2024-08-19",
        "EnDate": PERIOD.isoformat(),
        "FrgnBal": value,
        "FrgnBuy": 999,
        "FrgnSell": 1,
        "IndBal": 0,
        "InvTrBal": -10,
        "FutureField": None,
    }


def test_grouping_retains_unknown_fields_missing_values_and_all_publication_rows():
    rows = [flow(FIRST, 100), flow(REVISED, None)]
    grouped = jquants_flows.group_publications(rows, start=FIRST, end=REVISED)
    assert grouped == {FIRST: [rows[0]], REVISED: [rows[1]]}
    same_publication = {**rows[1], "EnDate": "2024-08-30"}
    assert jquants_flows.group_publications(
        [rows[1], same_publication], start=REVISED, end=REVISED
    )[REVISED] == [rows[1], same_publication]
    index = {"Date": FIRST.isoformat(), "O": 1, "H": 3, "L": None, "C": 2, "NewField": "raw"}
    assert jquants_indices.group_index_records([index], start=FIRST, end=FIRST) == {FIRST: [index]}


@pytest.mark.parametrize(
    "changes", [{"Section": "TSEStandard"}, {"PubDate": None}, {"PubDate": "2024-08-30"}]
)
def test_flow_grouping_does_not_redate_or_mix_another_response(changes):
    with pytest.raises(ProviderFetchStoppedError):
        jquants_flows.group_publications([{**flow(FIRST, 100), **changes}], start=FIRST, end=FIRST)


@pytest.mark.parametrize("value", [None, "", "2024-08-30"])
def test_index_grouping_does_not_redate_missing_or_other_days(value):
    with pytest.raises(ProviderFetchStoppedError):
        jquants_indices.group_index_records([{"Date": value, "C": 2}], start=FIRST, end=FIRST)


def test_flow_reader_selects_publication_before_value_and_keeps_period_freshness(tmp_path):
    path = tmp_path / "indicators.sqlite"
    definitions = load_definitions()
    foreign = definitions.by_id()["jp.foreign_flows"]
    with closing(db.initialize_database(path)) as connection:
        for published, value in [(FIRST, 100), (REVISED, 200)]:
            store_snapshot(
                connection,
                source_id="jquants-trades-spec",
                observed_at=published,
                fetched_at=FETCHED,
                records=[flow(published, value)],
            )
        connection.commit()
        before = build_store_observation_reader(
            connection, series=definitions.series, vintage_cutoff=FIRST
        )
        after = build_store_observation_reader(
            connection, series=definitions.series, vintage_cutoff=REVISED
        )
        assert [item.value for item in before(foreign.series_id, PERIOD, PERIOD)] == [100]
        result = after(foreign.series_id, PERIOD, PERIOD)
        assert [item.value for item in result] == [200]
        assert result[0].observed_at == PERIOD
        assert result[0].period_start == date(2024, 8, 19)
        assert result[0].period_end == PERIOD
        assert result[0].vintage_at == datetime(2024, 9, 10, tzinfo=UTC)
        later = datetime(2026, 10, 17, tzinfo=UTC)
        store_snapshot(
            connection,
            source_id="jquants-trades-spec",
            observed_at=REVISED,
            fetched_at=later,
            records=[flow(REVISED, None)],
        )
        connection.commit()
        assert after(foreign.series_id, PERIOD, PERIOD) == ()
        assert [item.value for item in before(foreign.series_id, PERIOD, PERIOD)] == [100]
        records = next(
            read_snapshots(connection, REVISED, REVISED, source_id="jquants-trades-spec")
        ).records
        assert records[0]["FrgnBuy"] == 999
        assert records[0]["FrgnBal"] is None
    assert indicator_latest_observed_at(path) == PERIOD


def test_index_reader_uses_only_native_close_and_empty_response_hides_old_payload(tmp_path):
    path = tmp_path / "indicators.sqlite"
    definitions = load_definitions()
    with closing(db.initialize_database(path)) as connection:
        store_snapshot(
            connection,
            source_id="jquants-indices-topix",
            observed_at=FIRST,
            fetched_at=FETCHED,
            records=[{"Date": FIRST.isoformat(), "C": 2000, "O": 1900}],
        )
        connection.commit()
        reader = build_store_observation_reader(connection, series=definitions.series)
        assert [item.value for item in reader("jp.topix", FIRST, FIRST)] == [2000]
        store_snapshot(
            connection,
            source_id="jquants-indices-topix",
            observed_at=FIRST,
            fetched_at=datetime(2026, 10, 17, tzinfo=UTC),
            records=[],
        )
        connection.commit()
        assert reader("jp.topix", FIRST, FIRST) == ()
        store_snapshot(
            connection,
            source_id="jquants-indices-topix",
            observed_at=FIRST,
            fetched_at=datetime(2026, 10, 18, tzinfo=UTC),
            records=[{"Date": FIRST.isoformat(), "Close": 2000}],
        )
        connection.commit()
        assert reader("jp.topix", FIRST, FIRST) == ()


@pytest.mark.parametrize(
    ("module", "source", "series_id", "rows"),
    [
        (jquants_flows, jquants_flows.JQuantsFlowsSource, "jp.foreign_flows", [flow(FIRST, 0)]),
        (
            jquants_indices,
            jquants_indices.JQuantsIndicesSource,
            "jp.topix",
            [{"Date": FIRST.isoformat(), "C": 2000}],
        ),
    ],
)
@pytest.mark.parametrize("bad_response", [False, True])
def test_native_sdk_source_closes_response_and_session(
    module, source, series_id, rows, bad_response, monkeypatch
):
    response = Mock()
    response.json.return_value = {} if bad_response else {"data": rows}
    client = Mock(JQUANTS_API_BASE="https://example.invalid/v2")
    client._get.return_value = response
    monkeypatch.setattr(module, "_read_api_key", lambda: "synthetic-key")
    monkeypatch.setattr(module, "jquants_read_client", lambda *args, **kwargs: client)
    series = load_definitions().by_id()[series_id]
    if bad_response:
        with pytest.raises(ProviderFetchStoppedError):
            source().fetch_records(series, start=FIRST, end=FIRST, session=Mock())
    else:
        assert source().fetch_records(series, start=FIRST, end=FIRST, session=Mock()) == {
            FIRST: rows
        }
    response.close.assert_called_once()
    client._request_session.return_value.close.assert_called_once()


@pytest.mark.parametrize(
    ("values", "expected"), [([0], [0]), ([100, 200], None), ([None, 100], [])]
)
def test_flow_same_publication_does_not_choose_an_arbitrary_balance(tmp_path, values, expected):
    path = tmp_path / "indicators.sqlite"
    definitions = load_definitions()
    with closing(db.initialize_database(path)) as connection:
        store_snapshot(
            connection,
            source_id="jquants-trades-spec",
            observed_at=FIRST,
            fetched_at=FETCHED,
            records=[flow(FIRST, value) for value in values],
        )
        connection.commit()
        reader = build_store_observation_reader(
            connection, series=definitions.series, vintage_cutoff=FIRST
        )
        if expected is None:
            with pytest.raises(ValueError, match="conflicting published balances"):
                reader("jp.foreign_flows", PERIOD, PERIOD)
        else:
            assert [item.value for item in reader("jp.foreign_flows", PERIOD, PERIOD)] == expected
        assert len(
            next(read_snapshots(connection, FIRST, FIRST, source_id="jquants-trades-spec")).records
        ) == len(values)
