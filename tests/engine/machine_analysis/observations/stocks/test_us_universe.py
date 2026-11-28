from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from baibai_engine.data_collection.providers.alpaca import DailyBarsQuery
from baibai_engine.data_collection.providers.nasdaq_symbols import read_us_symbol_directory
from baibai_engine.machine_analysis.observations.stocks.us_universe import (
    RakutenOrdinaryRow,
    RakutenRestrictionRow,
    cover_rakuten_us_ordinary_shares,
    parse_rakuten_ordinary_snapshot,
    rakuten_listing_key,
)

_NASDAQ = b"""Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
MSTA|Mesa Group Inc. - Class A Common Stock|Q|N|N|100|N|N
MSTB|Mesa Group Inc. - Class B Common Stock|Q|N|N|100|N|N
FUND|Mesa ETF|G|N|N|100|Y|N
PMF|Mesa NextShares|S|N|N|100|N|Y
TEST|Market Test Issue|Q|Y|N|100|N|N
File Creation Time: 1127202618:01|||||||
"""
_OTHER = b"""ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
ACTP|Prairie Inc. Common Stock|N|PR.B|N|100|N|PRB
CT|Capital Technologies, Inc.|N|CT|N|100|N|CT
ARC|Plains ETF|P|ARC|Y|100|N|ARC
NTST|Test Issue|N|NTST|N|100|Y|NTST
File Creation Time: 1127202618:01||||||
"""


@pytest.mark.parametrize(
    ("ticker", "exchange", "expected"),
    [
        ("BRK A", "NYSE", ("XNYS", "BRK.A")),
        ("BRK B", "NYSE", ("XNYS", "BRK.B")),
        ("BRK.B", "NYSE", ("XNYS", "BRK.B")),
        ("BRK B", "NASDAQ", ("XNAS", "BRK B")),
        ("LEN P", "NYSE", ("XNYS", "LEN P")),
        ("BW", "NYSE American", None),
        ("BW", "OTC", None),
    ],
)
def test_rakuten_listing_key_only_applies_supported_native_aliases(ticker, exchange, expected):
    assert rakuten_listing_key(ticker, exchange) == expected


def _directories():
    return (
        read_us_symbol_directory(_NASDAQ, file="nasdaqlisted.txt"),
        read_us_symbol_directory(_OTHER, file="otherlisted.txt"),
    )


def test_native_directories_keep_separate_share_classes_and_choose_the_nyse_quote_alias():
    nasdaq, nyse = _directories()
    assert nasdaq.creation_stamp == nyse.creation_stamp == "1127202618:01"
    listings = {row.ticker: row for row in (*nasdaq.listings, *nyse.listings)}
    assert set(listings) == {"MSTA", "MSTB", "FUND", "PMF", "PR.B", "CT"}
    assert listings["MSTA"].security_name.endswith("Class A Common Stock")
    assert listings["MSTB"].security_name.endswith("Class B Common Stock")
    assert listings["PR.B"].venue == "XNYS"
    assert not listings["CT"].fund_flag  # The NYSE name need not say "Common Stock".


@pytest.mark.parametrize(
    "body",
    [
        _NASDAQ.split(b"File Creation Time:")[0],
        _NASDAQ.replace(b"1127202618:01", b"1131202618:01"),
        _NASDAQ.replace(b"|ETF|NextShares", b"|Fund|NextShares"),
        _NASDAQ.replace(b"|Q|N|N|100|N|N", b"|Q|N|N|100|"),
        _NASDAQ.replace(b"|Q|N|N|100|N|N", b"|P|N|N|100|N|N"),
        _NASDAQ.replace(b"|Q|N|N|100|N|N", b"|Q|N|N|100|?|N"),
        _NASDAQ.replace(b"MSTB|", b"MSTA|"),
        _OTHER,
    ],
)
def test_bad_or_wrong_native_directory_is_not_interpreted_as_an_empty_universe(body):
    with pytest.raises(ValueError, match=r"Nasdaq Trader|Nasdaq listing tier"):
        read_us_symbol_directory(body, file="nasdaqlisted.txt")


def test_only_exact_current_cash_listings_enter_the_existing_alpaca_observation_query():
    rows = (
        RakutenOrdinaryRow("MSTA", "NASDAQ", "buyable"),
        RakutenOrdinaryRow("MSTB", "NASDAQ", "buyable"),
        RakutenOrdinaryRow("MSTA", "NYSE", "buyable"),
        RakutenOrdinaryRow("PR.B", "NYSE", "buyable"),
        RakutenOrdinaryRow("CT", "NYSE", "buyable"),
        RakutenOrdinaryRow("DEFER", "NYSE", "unknown"),
        RakutenOrdinaryRow("SELL", "NYSE", "sell_only"),
        RakutenOrdinaryRow("PR  B", "NYSE", "buyable"),
        RakutenOrdinaryRow("FUND", "NASDAQ", "buyable"),
        RakutenOrdinaryRow("PMF", "NASDAQ", "buyable"),
        RakutenOrdinaryRow("OTHER", "Cboe", "buyable"),
    )
    results = cover_rakuten_us_ordinary_shares(
        reversed(rows), reported_count=len(rows), directories=_directories()
    )
    assert len(results) == len(rows)
    assert {(r.broker_exchange, r.ticker): r.state for r in results} == {
        ("Cboe", "OTHER"): "unsupported_venue",
        ("NASDAQ", "FUND"): "exchange_fund",
        ("NASDAQ", "PMF"): "exchange_fund",
        ("NASDAQ", "MSTA"): "eligible",
        ("NASDAQ", "MSTB"): "eligible",
        ("NYSE", "CT"): "eligible",
        ("NYSE", "DEFER"): "unknown_buyability",
        ("NYSE", "MSTA"): "no_matching_listing",
        ("NYSE", "PR  B"): "unmapped_alpaca_symbol",
        ("NYSE", "PR.B"): "eligible",
        ("NYSE", "SELL"): "sell_only",
    }
    assert results == cover_rakuten_us_ordinary_shares(
        rows, reported_count=len(rows), directories=reversed(_directories())
    )
    symbols = tuple(row.alpaca_symbol for row in results if row.alpaca_symbol is not None)
    day = date(2026, 11, 27)
    params = parse_qs(urlsplit(DailyBarsQuery(symbols, day, day, "-", "split").url()).query)
    assert symbols == ("MSTA", "MSTB", "CT", "PR.B")
    assert params["symbols"] == [",".join(symbols)]
    assert params["feed"] == ["iex"]


def test_missing_current_listing_file_is_a_coverage_gap_not_a_different_venue_match():
    result = cover_rakuten_us_ordinary_shares(
        (RakutenOrdinaryRow("PR.B", "NYSE", "buyable"),),
        reported_count=1,
        directories=(_directories()[0],),
    )
    assert result[0].state == "missing_listing_directory"
    assert result[0].security_name is None


@pytest.mark.parametrize("reported_count", [0, 2, True])
def test_partial_or_unverified_broker_scope_fails(reported_count):
    with pytest.raises(ValueError, match="incomplete Rakuten"):
        cover_rakuten_us_ordinary_shares(
            (RakutenOrdinaryRow("MSTA", "NASDAQ", "buyable"),),
            reported_count=reported_count,
            directories=_directories(),
        )


def test_duplicate_broker_row_does_not_hide_a_missing_paginated_row():
    rows = (RakutenOrdinaryRow("PR.B", "NYSE", "buyable"),) * 2
    with pytest.raises(ValueError, match="duplicate Rakuten"):
        cover_rakuten_us_ordinary_shares(rows, reported_count=2, directories=_directories())


def test_no_or_repeated_directory_is_not_an_observation():
    rows = (RakutenOrdinaryRow("PR.B", "NYSE", "buyable"),)
    nasdaq, _ = _directories()
    for scope in ((), (nasdaq, nasdaq)):
        with pytest.raises(ValueError, match="missing or repeated"):
            cover_rakuten_us_ordinary_shares(rows, reported_count=1, directories=scope)


def test_exchange_transfer_overlap_is_not_a_unique_match():
    nasdaq, _ = _directories()
    stale_nyse = read_us_symbol_directory(
        _OTHER.replace(
            b"CT|Capital Technologies, Inc.|N|CT|N|100|N|CT",
            b"MSTA|Mesa Group Inc. Common Stock|N|MSTA|N|100|N|MSTA",
        ),
        file="otherlisted.txt",
    )
    rows = (RakutenOrdinaryRow("MSTA", "NASDAQ", "buyable"),)
    result = cover_rakuten_us_ordinary_shares(
        rows, reported_count=1, directories=(nasdaq, stale_nyse)
    )
    assert result[0].state == "ambiguous_primary_listing"
    assert result[0].security_name is None


def test_unknown_literal_is_not_mistaken_for_an_explicit_buyable_row():
    rows = (RakutenOrdinaryRow("PR.B", "NYSE", "tradeable"),)
    with pytest.raises(ValueError, match="unknown Rakuten buyability label"):
        cover_rakuten_us_ordinary_shares(rows, reported_count=1, directories=_directories())


def _with_nyse_classes():
    securities = (
        b"BRK.A|Berkshire Hathaway Inc. Common Stock|N|BRK.A|N|1|N|BRK.A\n"
        b"BRK.B|Berkshire Hathaway Inc. New Common Stock|N|BRK.B|N|40|N|BRK.B\n"
        b"BF.B|Brown Forman Inc Class B Common Stock|N|BF.B|N|100|N|BF.B\n"
        b"LEN.B|Lennar Corporation Class B|N|LEN.B|N|100|N|LEN.B\n"
        b"PREF$B|Fixture Inc. Class B Preferred Stock|N|PREFpB|N|100|N|PREF-B\n"
        b"FICT.B|Fixture ETF|N|FICT.B|Y|100|N|FICT.B\n"
        b"WRT.W|Fixture Inc. Warrants|N|WRT.WS.B|N|100|N|WRT+B\n"
    )
    return (
        _directories()[0],
        read_us_symbol_directory(
            _OTHER.replace(b"File Creation Time:", securities + b"File Creation Time:"),
            file="otherlisted.txt",
        ),
    )


def test_rakuten_nyse_class_label_uses_exact_cqs_alias_without_rewriting_broker_field():
    rows = (
        RakutenOrdinaryRow("BRK B", "NYSE", "buyable"),
        RakutenOrdinaryRow("BRK A", "NYSE", "buyable"),
        RakutenOrdinaryRow("BF B", "NYSE", "buyable"),
        RakutenOrdinaryRow("BRK B", "NASDAQ", "buyable"),
        RakutenOrdinaryRow("PR C", "NYSE", "buyable"),
        RakutenOrdinaryRow("WRT B", "NYSE", "buyable"),
        RakutenOrdinaryRow("PREF B", "NYSE", "buyable"),
        RakutenOrdinaryRow("LEN B", "NYSE", "buyable"),
        RakutenOrdinaryRow("FICT B", "NYSE", "buyable"),
    )
    coverage = cover_rakuten_us_ordinary_shares(
        rows, reported_count=len(rows), directories=_with_nyse_classes()
    )
    assert {(r.broker_exchange, r.ticker): (r.state, r.alpaca_symbol) for r in coverage} == {
        ("NYSE", "BRK B"): ("eligible", "BRK.B"),
        ("NYSE", "BRK A"): ("eligible", "BRK.A"),
        ("NYSE", "BF B"): ("eligible", "BF.B"),
        ("NASDAQ", "BRK B"): ("unmapped_alpaca_symbol", None),
        ("NYSE", "PR C"): ("unmapped_alpaca_symbol", None),
        ("NYSE", "WRT B"): ("no_matching_listing", None),
        ("NYSE", "PREF B"): ("no_matching_listing", None),
        ("NYSE", "LEN B"): ("eligible", "LEN.B"),
        ("NYSE", "FICT B"): ("exchange_fund", None),
    }


def test_a_confirmed_alias_cannot_double_count_or_disagree_on_buyability():
    pairs = (
        RakutenOrdinaryRow("BRK B", "NYSE", "sell_only"),
        RakutenOrdinaryRow("BRK.B", "NYSE", "buyable"),
    )
    for rows in (pairs, tuple(reversed(pairs))):
        with pytest.raises(ValueError, match="duplicate Rakuten"):
            cover_rakuten_us_ordinary_shares(
                rows, reported_count=2, directories=_with_nyse_classes()
            )


_NY_SESSION = date(2026, 11, 27)
_CATALOG_RECEIVED = datetime.fromisoformat("2026-11-28T11:55:00+09:00")
_EXCLUSIONS_RECEIVED = datetime.fromisoformat("2026-11-27T22:07:00-05:00")
_CATALOG = "ticker,exchange\nMSTA,NASDAQ\nMSTB,NASDAQ\nPR B,NYSE\nCT,NYSE\n"
_EXCLUSION_HEADER = "ticker,exchange,restriction\n"
_TRADABLE = "cash_ordinary_tradable"
_CATALOG_ONLY = "cash_ordinary_catalog"


def _parsed_snapshot(
    *,
    mode=_TRADABLE,
    catalog=_CATALOG,
    count=4,
    exclusions=None,
    excluded_count=None,
    target=_NY_SESSION,
    catalog_time=_CATALOG_RECEIVED,
    restrictions_time=_EXCLUSIONS_RECEIVED,
):
    return parse_rakuten_ordinary_snapshot(
        catalog,
        catalog_mode=mode,
        effective_session=target,
        catalog_received_at=catalog_time,
        reported_count=count,
        exclusions_csv=exclusions,
        exclusions_received_at=restrictions_time if exclusions is not None else None,
        exclusions_reported_count=excluded_count,
    )


def _coverage(snapshot):
    return cover_rakuten_us_ordinary_shares(
        snapshot.rows, reported_count=snapshot.reported_count, directories=_directories()
    )


def test_complete_cash_catalog_and_official_restrictions_join_before_the_alpaca_query():
    exclusions = (
        f"{_EXCLUSION_HEADER}MSTB,NASDAQ,sell_only\nPR.B,NYSE,buy_suspended\n"
        "MSTA,NYSE,sell_only\nOLD,OTC,sell_only\n"
    )
    snapshot = _parsed_snapshot(
        catalog="\ufeff" + _CATALOG.replace("\n", "\r\n"),
        exclusions=exclusions,
        excluded_count=4,
    )
    assert (snapshot.catalog_mode, snapshot.effective_session) == (_TRADABLE, _NY_SESSION)
    assert snapshot.catalog_received_at == _CATALOG_RECEIVED
    assert snapshot.catalog_received_at.tzinfo is UTC
    assert snapshot.exclusions_received_at == _EXCLUSIONS_RECEIVED
    assert snapshot.exclusions_received_at.tzinfo is UTC
    assert (snapshot.reported_count, snapshot.exclusions_reported_count) == (4, 4)
    assert snapshot.restriction_only == (
        RakutenRestrictionRow("MSTA", "NYSE", "sell_only"),
        RakutenRestrictionRow("OLD", "OTC", "sell_only"),
    )
    assert [(row.ticker, row.state, row.alpaca_symbol) for row in _coverage(snapshot)] == [
        ("MSTA", "eligible", "MSTA"),
        ("MSTB", "sell_only", None),
        ("CT", "eligible", "CT"),
        ("PR B", "buy_suspended", None),
    ]
    symbols = tuple(row.alpaca_symbol for row in _coverage(snapshot) if row.alpaca_symbol)
    day = date(2026, 11, 27)
    params = parse_qs(urlsplit(DailyBarsQuery(symbols, day, day, "-", "split").url()).query)
    assert params["symbols"] == ["MSTA,CT"]


def test_catalog_only_never_derives_positive_status_even_with_an_empty_exclusion_capture():
    snapshot = _parsed_snapshot(mode=_CATALOG_ONLY, exclusions=_EXCLUSION_HEADER, excluded_count=0)
    assert {row.state for row in _coverage(snapshot)} == {"unknown_buyability"}
    assert all(row.alpaca_symbol is None for row in _coverage(snapshot))


def test_completed_zero_restrictions_is_not_a_missing_capture():
    with pytest.raises(ValueError, match="incomplete Rakuten buy restriction"):
        _parsed_snapshot()
    snapshot = _parsed_snapshot(exclusions=_EXCLUSION_HEADER, excluded_count=0)
    assert all(row.state == "eligible" for row in _coverage(snapshot))
    assert snapshot.restriction_only == ()
    assert snapshot.exclusions_reported_count == 0
    assert snapshot.exclusions_received_at == _EXCLUSIONS_RECEIVED


def test_restriction_only_rows_do_not_expand_the_ordinary_listing_or_price_denominator():
    exclusion_rows = (
        "PR B,NYSE,buy_suspended",
        "PR.B,NASDAQ,sell_only",
        "ZRESTR,NYSE,sell_only",
    )

    def with_restrictions(lines):
        return _parsed_snapshot(
            catalog="ticker,exchange\nMSTA,NASDAQ\n",
            count=1,
            exclusions=_EXCLUSION_HEADER + "\n".join(lines) + "\n",
            excluded_count=len(exclusion_rows),
        )

    snapshot = with_restrictions(exclusion_rows)
    assert snapshot == with_restrictions(reversed(exclusion_rows))
    assert snapshot.restriction_only == (
        RakutenRestrictionRow("PR.B", "NASDAQ", "sell_only"),
        RakutenRestrictionRow("PR B", "NYSE", "buy_suspended"),
        RakutenRestrictionRow("ZRESTR", "NYSE", "sell_only"),
    )
    assert snapshot.rows == (RakutenOrdinaryRow("MSTA", "NASDAQ", "buyable"),)
    assert [row.alpaca_symbol for row in _coverage(snapshot)] == ["MSTA"]


def test_informational_missing_restrictions_remain_explicitly_unknown():
    snapshot = _parsed_snapshot(mode=_CATALOG_ONLY)
    assert snapshot.exclusions_received_at is None
    assert snapshot.exclusions_reported_count is None
    assert snapshot.restriction_only == ()
    assert {row.state for row in _coverage(snapshot)} == {"unknown_buyability"}


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"mode": "cash_ordinary_tradable_nasdaq_only"}, "snapshot mode"),
        ({"count": 5}, "incomplete Rakuten snapshot"),
        ({"count": True}, "incomplete Rakuten snapshot"),
        ({"catalog": "ticker,exchange\n", "count": 0}, "empty Rakuten ordinary catalog"),
        ({"catalog": "ticker,exchange,buy_status\n", "count": 0}, "CSV header"),
        ({"catalog": _CATALOG.replace("MSTA,NASDAQ", "MSTA,NASDAQ,○")}, "CSV row"),
        ({"catalog": _CATALOG.replace("MSTA,NASDAQ", "MSTB,NASDAQ")}, "duplicate Rakuten"),
        ({"catalog": _CATALOG.replace("PR B,NYSE", '"PR B,NYSE')}, "snapshot CSV"),
        ({"excluded_count": 1}, "incomplete Rakuten snapshot"),
        ({"excluded_count": None}, "incomplete Rakuten buy restriction"),
        (
            {"exclusions": _EXCLUSION_HEADER + "MSTA,NASDAQ,buyable\n", "excluded_count": 1},
            "unknown Rakuten buy restriction",
        ),
        (
            {
                "exclusions": _EXCLUSION_HEADER + "PR B,NYSE,sell_only\nPR.B,NYSE,buy_suspended\n",
                "excluded_count": 2,
            },
            "duplicate Rakuten",
        ),
    ],
)
def test_incomplete_or_wrong_snapshot_has_no_screening_result(overrides, error):
    arguments = {"exclusions": _EXCLUSION_HEADER, "excluded_count": 0, **overrides}
    with pytest.raises(ValueError, match=error):
        _parsed_snapshot(**arguments)


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"target": _CATALOG_RECEIVED}, "requires a target NY session date"),
        ({"target": date(2026, 11, 28)}, "receipt is outside its target NY session date"),
        ({"catalog_time": _CATALOG_RECEIVED - timedelta(days=1)}, "outside its target NY session"),
        (
            {"restrictions_time": _EXCLUSIONS_RECEIVED + timedelta(days=1)},
            "outside its target NY session",
        ),
        (
            {"restrictions_time": _EXCLUSIONS_RECEIVED - timedelta(days=1)},
            "outside its target NY session",
        ),
        ({"catalog_time": _CATALOG_RECEIVED.replace(tzinfo=None)}, "receipt time.*UTC offset"),
        (
            {"restrictions_time": _EXCLUSIONS_RECEIVED.replace(tzinfo=None)},
            "receipt time.*UTC offset",
        ),
        ({"restrictions_time": None}, "incomplete Rakuten buy restriction"),
    ],
)
def test_rakuten_snapshot_requires_a_complete_pair_of_same_session_source_clocks(overrides, error):
    with pytest.raises(ValueError, match=error):
        _parsed_snapshot(exclusions=_EXCLUSION_HEADER, excluded_count=0, **overrides)


def test_exclusion_clock_without_an_independently_counted_capture_is_not_complete():
    with pytest.raises(ValueError, match="incomplete Rakuten buy restriction"):
        parse_rakuten_ordinary_snapshot(
            _CATALOG,
            catalog_mode=_CATALOG_ONLY,
            effective_session=_NY_SESSION,
            catalog_received_at=_CATALOG_RECEIVED,
            reported_count=4,
            exclusions_received_at=_EXCLUSIONS_RECEIVED,
        )
