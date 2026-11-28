"""The V53 runtime consumes current classification or truthfully reports its absence."""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta

import pytest
from tests.helpers.financial_native import fixed_empty_financial_input
from tests.helpers.model_generation import materialize_generation
from tests.helpers.screening_sqlite import insert_daily_bars_from_closes

from baibai_engine.machine_analysis.generation.inputs import (
    PinnedGenerationMaster,
    read_and_generate_models,
)
from baibai_engine.machine_analysis.generation.runners.market_snapshot import (
    build_market_snapshot,
    prepare_market_context_from_bars,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.observations.prices.adjusted_prices import read_price_bars
from baibai_engine.machine_analysis.observations.stocks.current_reader import read_current_jp_master
from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    JapanListingAttributes,
    ListingMasterRow,
    ListingSlice,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    CurrentListingMarket,
)
from baibai_engine.primitives.time import JST
from baibai_engine.storage.snapshots.writer import sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

_FRI = date(2026, 10, 23)
_SUN = _FRI + timedelta(days=2)
_FRI_CLOSE = datetime(2026, 10, 23, 18, tzinfo=JST)


def _market(path, *, lagging=False):
    close_day = _FRI - timedelta(days=lagging)
    for ticker in ("1321", "1234"):
        insert_daily_bars_from_closes(path, ticker, [100.0] * 65 + [150.0], end_date=close_day)
    with closing(open_connection(path)) as db, db:
        db.executemany(
            "INSERT OR REPLACE INTO jquants_market_calendar VALUES (?, ?)",
            [(str(_FRI), "1"), (str(_FRI + timedelta(days=1)), "0"), (str(_SUN), "3")],
        )
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='jquants_master_snapshots'"
            ).fetchone()
            is None
        )
    return path


def _section(*, session=_FRI, received_at=_FRI_CLOSE):
    return CurrentListingMarket(
        ListingSlice(
            "JP",
            session,
            (
                ListingMasterRow(
                    ("XTKS", "12340"),
                    "合成企業",
                    "common_stock",
                    "listed",
                    japan=JapanListingAttributes("011", "プライム", "機械"),
                ),
            ),
        ),
        "jquants",
        received_at,
        1,
    )


def test_generation_requires_current_section_even_when_subjects_are_explicit(tmp_path):
    market = _market(tmp_path / "market.sqlite")
    financial = fixed_empty_financial_input(tmp_path / "financial", _FRI)
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=tmp_path / "mirror") as seal:
        arguments = dict(
            as_of=_FRI,
            evaluation_time=_FRI_CLOSE,
            market_snapshot=seal,
            generation_id="current-fixture",
            market_release=InputRelease(release_id="same", manifest_sha256="a" * 64),
            economic_input=None,
            financial_input=financial,
            ttm_rules=DEFAULT_TTM_RULES,
            tickers=("1234",),
        )
        with pytest.raises(TypeError, match="current_master"):
            read_and_generate_models(**arguments)
        for invalid in (
            _section(session=_FRI - timedelta(days=1)),
            _section(received_at=_FRI_CLOSE + timedelta(microseconds=1)),
        ):
            with pytest.raises(ValueError, match="does not match the generation seal and session"):
                read_and_generate_models(
                    **arguments, current_master=PinnedGenerationMaster(invalid, "market/current#JP")
                )
        result = materialize_generation(
            read_and_generate_models(
                **arguments, current_master=PinnedGenerationMaster(_section(), "market/current#JP")
            )
        )
        assert result.inputs["master"].kind == "current_listing_master"
        assert result.result("market_context", "1234").payload["master"]["sector_33"] == "機械"


@pytest.mark.parametrize("lagging", [False, True])
def test_raw_and_prepared_market_context_only_classify_exact_observed_price_day(tmp_path, lagging):
    market = _market(tmp_path / "market.sqlite", lagging=lagging)
    arguments = dict(
        sqlite_path=market,
        asof_date=_SUN,
        history_weeks=2,
        min_breadth_sample=1,
    )
    without = build_market_snapshot(**arguments)
    assert without["master_snapshot_date"] is None
    assert without["sectors"] == []
    assert without["points"][-1]["benchmark_return_20d"] == 0.5
    assert without["points"][-1]["breadth_pct_above_ma20"] is None

    now = _FRI_CLOSE + timedelta(days=2)

    def classification(connection, day):
        return read_current_jp_master(
            _section(), calendar=connection, as_of=day, evaluation_time=now
        )

    with closing(open_connection(market)) as db:
        current = classification(db, _SUN)
    prepared = prepare_market_context_from_bars(
        sqlite_path=market,
        asof_date=_SUN,
        master=current,
        bars_by_ticker=read_price_bars(market, start=date.min, end=_SUN).items(),
        subjects=("1234",),
        history_weeks=2,
    )
    raw = build_market_snapshot(**arguments, read_current_master=classification)
    assert raw == build_market_snapshot(**arguments, prepared=prepared)
    assert raw["master_snapshot_date"] == str(_FRI)
    assert raw["points"][0]["breadth_sample_size"] == 0
    assert raw["points"][-1]["benchmark_return_20d"] == 0.5
    if lagging:
        assert raw["points"][-1]["breadth_pct_above_ma20"] is None
        assert raw["sectors"] == []
    else:
        assert raw["points"][-1]["breadth_pct_above_ma20"] == 1.0
        (sector,) = raw["sectors"]
        assert sector["observation_date"] == str(_FRI)
        assert sector["pct_above_ma20"] == 1.0
