"""Analysis boundary rejection and real SQLite transaction isolation/failure."""

import sqlite3
from datetime import timedelta

import pytest
from tests.helpers.external_assessment_observations import RECEIVED, rating, valuation

import baibai_engine.machine_analysis.observations.external_assessments.reader as reader
from baibai_engine.machine_analysis.models.external_assessment.analysis import analyze
from baibai_engine.machine_analysis.observations.external_assessments.contract import (
    ANALYST_RATING_TABLE,
    MODEL_VALUATION_TABLE,
)
from baibai_engine.machine_analysis.observations.external_assessments.history import (
    AnalystRatingHistory,
    ModelValuationHistory,
    ObservationHistory,
)
from baibai_engine.machine_analysis.observations.external_assessments.store import save_observations
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def test_analysis_rejects_cutoff_later_than_evaluation():
    history = ObservationHistory(ticker="1234", cutoff=RECEIVED + timedelta(seconds=1))
    with pytest.raises(ValueError, match="cutoff cannot be later than evaluation"):
        analyze(history, evaluated_at=RECEIVED)


def test_analysis_rejects_another_ticker_in_active_input_series():
    history = ObservationHistory(
        ticker="1234",
        cutoff=RECEIVED,
        model_valuations=(ModelValuationHistory(latest=valuation(ticker="9999")),),
    )
    with pytest.raises(ValueError, match="outside ticker/cutoff"):
        analyze(history, evaluated_at=RECEIVED)


@pytest.mark.parametrize("previous_offset", [1, 2])
def test_previous_must_be_strictly_earlier_than_latest(previous_offset):
    latest_at = RECEIVED + timedelta(hours=1)
    previous_at = RECEIVED + timedelta(hours=previous_offset)
    cutoff = RECEIVED + timedelta(hours=2)
    history = ObservationHistory(
        ticker="1234",
        cutoff=cutoff,
        model_valuations=(
            ModelValuationHistory(latest=valuation(latest_at), previous=valuation(previous_at)),
        ),
    )
    with pytest.raises(ValueError, match="previous observation must precede latest"):
        analyze(history, evaluated_at=cutoff)


def test_comparable_decreases_keep_signed_delta_and_both_input_references():
    later = RECEIVED + timedelta(hours=1)
    history = ObservationHistory(
        ticker="1234",
        cutoff=later,
        model_valuations=(
            ModelValuationHistory(
                previous=valuation(),
                latest=valuation(later, consensus_target=1100),
            ),
        ),
        analyst_ratings=(
            AnalystRatingHistory(
                previous=rating(),
                latest=rating(later, rating=3),
            ),
        ),
    )
    result = analyze(history, evaluated_at=later)
    assert result.model_valuations[0].inputs == history.model_valuations[0]
    assert result.analyst_ratings == ()
    assert result.model_valuations[0].comparisons[0].state == "decreased"
    assert result.model_valuations[0].comparisons[0].comparable
    assert result.model_valuations[0].comparisons[0].delta == -100


def test_both_datasets_read_one_snapshot_despite_an_interleaved_wal_commit(tmp_path, monkeypatch):
    db = tmp_path / "market.sqlite"
    first_valuation, first_rating = valuation(), rating()
    later = RECEIVED + timedelta(hours=1)
    next_valuation, next_rating = valuation(later, consensus_target=1300), rating(later, rating=4)
    save_observations(db, [first_valuation, first_rating])
    connection = open_connection(db)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    finally:
        connection.close()
    latest_rows = reader._latest_rows
    writes = []

    def read_then_commit(conn, table, columns, ticker, cutoff):
        rows = latest_rows(conn, table, columns, ticker, cutoff)
        if table == MODEL_VALUATION_TABLE and not writes:
            # Commit a complete new pair between the two reads, without threads.
            writes.append(save_observations(db, [next_valuation, next_rating]))
        return rows

    monkeypatch.setattr(reader, "_latest_rows", read_then_commit)
    snapshot = reader.read_observations(db, "1234", later)
    assert writes == [2]
    assert snapshot.model_valuations[0].latest == first_valuation
    assert snapshot.analyst_ratings[0].latest == first_rating
    assert snapshot.model_valuations[0].previous is None
    assert snapshot.analyst_ratings[0].previous is None
    # The committed rows are visible when a new read snapshot starts.
    next_snapshot = reader.read_observations(db, "1234", later)
    assert next_snapshot.model_valuations[0].latest == next_valuation
    assert next_snapshot.analyst_ratings[0].latest == next_rating


def test_storage_abort_rolls_back_prior_inserts_in_every_dataset(tmp_path):
    db = tmp_path / "market.sqlite"
    first_valuation, first_rating = valuation(), rating()
    save_observations(db, [first_valuation, first_rating])
    connection = open_connection(db)
    try:
        connection.execute(
            f"CREATE TRIGGER synthetic_storage_failure BEFORE INSERT ON {ANALYST_RATING_TABLE} "
            "BEGIN SELECT RAISE(ABORT, 'synthetic storage abort'); END"
        )
        connection.commit()
    finally:
        connection.close()
    later = RECEIVED + timedelta(hours=1)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic storage abort"):
        save_observations(db, [valuation(later, consensus_target=1300), rating(later, rating=4)])
    history = reader.read_observations(db, "1234", later)
    assert history.model_valuations[0].latest == first_valuation
    assert history.analyst_ratings[0].latest == first_rating
    assert history.model_valuations[0].previous is None
    assert history.analyst_ratings[0].previous is None
