"""One current generation reader: bounded histories, held seal and transactional results."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.helpers.financial_native import fixed_empty_financial_input
from tests.helpers.lake_release import market_store
from tests.helpers.master_calendar_native import pinned_jp_listing, seed_listing_session

from baibai_engine.machine_analysis.generation import inputs
from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
from baibai_engine.machine_analysis.generation.query import read_generation_ids
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.observations.prices import adjusted_prices
from baibai_engine.storage.snapshots.writer import SealedSQLiteSnapshot, sealed_sqlite_snapshot

_DAY = date(2026, 4, 14)
_AT = datetime(2026, 4, 15, tzinfo=UTC)
_SUBJECTS = tuple(f"{ticker:04}" for ticker in range(2000, 2065))


def _market(tmp_path: Path, subjects: tuple[str, ...]) -> Path:
    path = market_store(
        tmp_path / "market.sqlite",
        daily_bars=tuple((t, str(_DAY), 100.0, 1020.0) for t in (*subjects, "1321")),
        short_sale_reports=(),
    )
    seed_listing_session(path, _DAY)
    return path


def _generation(tmp_path: Path, seal: SealedSQLiteSnapshot, subjects: tuple[str, ...]):
    return inputs.read_and_generate_models(
        generation_id="bounded-price-proof",
        market_snapshot=seal,
        market_release=InputRelease(release_id="fixed", manifest_sha256="a" * 64),
        economic_input=None,
        financial_input=fixed_empty_financial_input(tmp_path / "finance", _DAY),
        current_master=pinned_jp_listing(as_of=_DAY, tickers=subjects),
        as_of=_DAY,
        evaluation_time=_AT,
        ttm_rules=DEFAULT_TTM_RULES,
    )


def test_batch_reader_delivers_ordered_full_series_and_no_open_connection(tmp_path, mocker):
    market = _market(tmp_path, _SUBJECTS)
    owner = mocker.spy(adjusted_prices, "read_adjusted_bars")
    batches = tuple(
        adjusted_prices.iter_price_bar_batches(
            market,
            start=date.min,
            end=_DAY,
            tickers=("9999", *_SUBJECTS[::-1], _SUBJECTS[0]),
        )
    )
    assert tuple(map(len, (tickers for tickers, _ in batches))) == (64, 2)
    assert tuple(ticker for tickers, _ in batches for ticker in tickers) == (*_SUBJECTS, "9999")
    assert tuple(ticker for _, bars in batches for ticker in bars) == _SUBJECTS
    assert all(
        call.kwargs["start"] == date.min and call.kwargs["end"] == _DAY
        for call in owner.call_args_list
    )
    for call in owner.call_args_list:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            call.args[0].execute("SELECT 1")


def test_corrupt_old_excluded_history_is_rejected_before_any_models(tmp_path):
    market = _market(tmp_path, ("2000",))
    with closing(sqlite3.connect(market)) as db, db:
        db.execute(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close,adjustment_factor) "
            "VALUES ('9999','2024-02-30',90.0,1.0)"
        )
    with (
        sealed_sqlite_snapshot(sqlite_path=market, mirror_root=tmp_path / "lake") as seal,
        pytest.raises(ValueError, match=r"day.*(out of range|must be in range)"),
    ):
        _generation(tmp_path, seal, ("2000",))


def test_expired_seal_refuses_runs_publication_without_a_header(tmp_path):
    market = _market(tmp_path, ("2000",))
    runs = tmp_path / "runs.sqlite"
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=tmp_path / "lake") as seal:
        generation = _generation(tmp_path, seal, ("2000",))
        assert seal.path.is_file()
    assert not seal.path.exists()
    with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):
        ModelGenerationStore(runs).publish_stream(generation)
    assert read_generation_ids(path=runs) == ()


def test_later_price_batch_failure_rolls_back_staged_generation(tmp_path, monkeypatch):
    market = _market(tmp_path, _SUBJECTS)
    runs = tmp_path / "runs.sqlite"
    original = inputs.iter_price_bar_batches
    seen: list[str] = []

    def falling(path: Path, *, start: date, end: date, tickers=None):
        batches = original(path, start=start, end=end, tickers=tickers)
        if tickers is None:
            yield from batches
        else:
            first = next(batches)
            assert len(first[0]) == 64
            yield first
            raise ValueError("synthetic later price read failure")

    monkeypatch.setattr(inputs, "iter_price_bar_batches", falling)
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=tmp_path / "lake") as seal:
        generation = _generation(tmp_path, seal, _SUBJECTS)

        def observed() -> Iterator[GeneratedModel]:
            for model in generation.models:
                seen.append(model.subject)
                yield model

        with pytest.raises(ValueError, match="synthetic later price read failure"):
            ModelGenerationStore(runs).publish_stream(replace(generation, models=observed()))
    assert set(seen) == set(_SUBJECTS[:64])
    assert read_generation_ids(path=runs) == ()
    with closing(sqlite3.connect(runs)) as db:
        assert db.execute("SELECT count(*) FROM generated_model").fetchone() == (0,)
