"""Synthetic received-time history and conservative same-series comparisons."""

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from baibai_engine.external_assessment.analysis import analyze
from baibai_engine.external_assessment.models import (
    AnalystRatingHistory,
    ModelValuationHistory,
    ObservationHistory,
)
from baibai_engine.external_assessment.observations.models import (
    AnalystRatingObservation,
    ModelValuationObservation,
)
from baibai_engine.external_assessment.reader import read_observations
from baibai_engine.external_assessment.store import ObservationConflictError, save_observations
from baibai_engine.storage.observations.schema import SQLiteSchemaError, open_connection

RECEIVED = datetime(2026, 10, 10, 16, 30, 0, 123456, tzinfo=UTC)


def valuation(at=RECEIVED, **changes):
    return ModelValuationObservation.model_validate(
        {
            "ticker": "1234",
            "fetched_at_utc": at,
            "distributor": "monex",
            "source_url": "https://example.invalid/model/1234",
            "parser_version": "synthetic-1",
            "consensus_target": 1200,
            "currency": "JPY",
            "unit": "currency_per_share",
            "share_basis": "synthetic-common-stock-2026",
            "consensus_target_origin": "ifis",
            "consensus_target_period": "12-month-target",
            "consensus_target_count": 4,
            **changes,
        }
    )


def rating(at=RECEIVED, **changes):
    return AnalystRatingObservation.model_validate(
        {
            "ticker": "1234",
            "fetched_at_utc": at,
            "distributor": "rakuten",
            "origin": "ifis",
            "source_url": "https://example.invalid/rating/1234",
            "parser_version": "synthetic-1",
            "rating": 3.5,
            "rating_scale_min": 1,
            "rating_scale_max": 5,
            "rating_scale_direction": "higher_is_positive",
            "rating_count": 4,
            **changes,
        }
    )


def comparison(previous, latest):
    history = ObservationHistory(
        ticker="1234",
        cutoff=latest.fetched_at_utc,
        model_valuations=(ModelValuationHistory(latest=latest, previous=previous),),
    )
    return analyze(history, evaluated_at=latest.fetched_at_utc)


def test_replay_exact_time_and_new_receive_keep_history(tmp_path):
    db = tmp_path / "market.sqlite"
    first = valuation(consensus_target_updated_label="10/09", per_company_forecast=None)
    later = valuation(RECEIVED + timedelta(hours=1))
    assert save_observations(db, [first, rating()]) == 2
    replay = ModelValuationObservation.model_validate(first.model_dump(mode="json"))
    assert replay == first
    assert save_observations(db, [replay]) == 0
    assert save_observations(db, [later]) == 1
    history = read_observations(db, "1234", later.fetched_at_utc)
    assert history.model_valuations[0].latest == later
    assert history.model_valuations[0].previous == first
    assert history.analyst_ratings[0].latest == rating()
    assert first.snapshot_date.isoformat() == "2026-10-11"
    assert first.fetched_at_utc.microsecond == 123456


def test_conflicting_replay_rolls_back_entire_multi_dataset_batch(tmp_path):
    db = tmp_path / "market.sqlite"
    first = valuation()
    save_observations(db, [first])
    with pytest.raises(ObservationConflictError, match="conflicting immutable"):
        save_observations(
            db,
            [rating(), valuation(RECEIVED + timedelta(hours=1)), valuation(consensus_target=1300)],
        )
    history = read_observations(db, "1234", RECEIVED + timedelta(days=1))
    assert history.model_valuations[0].latest == first
    assert history.model_valuations[0].previous is None
    assert history.analyst_ratings == ()


def test_cutoff_selects_latest_missing_instead_of_old_nonnull(tmp_path):
    db = tmp_path / "market.sqlite"
    first = valuation()
    missing = valuation(RECEIVED + timedelta(hours=1), consensus_target=None)
    future = valuation(RECEIVED + timedelta(hours=2), consensus_target=1300)
    save_observations(db, [first, missing, future])
    history = read_observations(db, "1234", missing.fetched_at_utc)
    assert history.model_valuations[0].latest == missing
    assert history.model_valuations[0].previous == first
    result = analyze(history, evaluated_at=future.fetched_at_utc)
    assert result.model_valuations[0].comparisons[0].state == "missing"
    assert result.model_valuations[0].comparisons[0].latest_value is None
    assert result.model_valuations[0].comparisons[0].previous_value == 1200
    assert (
        read_observations(db, "1234", RECEIVED - timedelta(microseconds=1)).model_valuations == ()
    )
    assert read_observations(db, "9999", future.fetched_at_utc).model_valuations == ()


def test_empty_current_store_and_missing_file_are_unobserved(tmp_path):
    db = tmp_path / "market.sqlite"
    assert read_observations(db, "1234", RECEIVED).analyst_ratings == ()
    assert not db.exists()
    open_connection(db).close()
    result = analyze(read_observations(db, "1234", RECEIVED), evaluated_at=RECEIVED)
    assert result.model_valuations == ()
    assert result.analyst_ratings[0].distributor == "rakuten"
    assert result.analyst_ratings[0].inputs.latest is None
    assert result.analyst_ratings[0].comparison.state == "not_observed"
    assert result.tradingview.availability == "not_observed"


def test_obsolete_shape_and_corrupt_sqlite_are_errors(tmp_path):
    db = tmp_path / "obsolete.sqlite"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE old_table(value TEXT)")
    with pytest.raises(SQLiteSchemaError):
        read_observations(db, "1234", RECEIVED)
    db.write_bytes(b"this is not SQLite")
    with pytest.raises(sqlite3.DatabaseError):
        read_observations(db, "1234", RECEIVED)


@pytest.mark.parametrize(
    ("field", "change", "reason"),
    [
        ("currency", None, "unknown_currency"),
        ("currency", "USD", "changed_currency"),
        ("unit", None, "unknown_unit"),
        ("share_basis", None, "unknown_share_basis"),
        ("share_basis", "split-2-for-1", "changed_share_basis"),
        ("consensus_target_period", "FY2027", "changed_consensus_target_period"),
        ("consensus_target_origin", None, "unknown_consensus_target_origin"),
    ],
)
def test_unknown_or_changed_basis_has_no_delta(field, change, reason):
    latest = valuation(RECEIVED + timedelta(days=1), consensus_target=1400, **{field: change})
    result = comparison(valuation(), latest)
    metric = result.model_valuations[0].comparisons[0]
    assert not metric.comparable
    assert metric.state == "not_comparable"
    assert metric.reason == reason
    assert metric.delta is None
    assert result.model_valuations[0].inputs.latest == latest
    assert result.method_version == "same-series-v3"


def test_same_value_recovery_and_count_change_are_distinct_from_increase():
    first = valuation()
    later_at = RECEIVED + timedelta(days=1)
    same = comparison(first, valuation(later_at)).model_valuations[0].comparisons[0]
    assert same.state == "same_value"
    assert same.delta == 0
    assert same.comparable
    increase = (
        comparison(first, valuation(later_at, consensus_target=1300))
        .model_valuations[0]
        .comparisons[0]
    )
    assert increase.state == "increased"
    assert increase.delta == 100
    recovery = (
        comparison(valuation(consensus_target=None), valuation(later_at))
        .model_valuations[0]
        .comparisons[0]
    )
    assert recovery.state == "recovered"
    assert recovery.delta is None
    count = (
        comparison(first, valuation(later_at, consensus_target=1300, consensus_target_count=5))
        .model_valuations[0]
        .comparisons[0]
    )
    assert count.state == "count_changed"
    assert count.delta is None
    assert count.latest_count == 5
    assert count.previous_count == 4


def test_rating_requires_known_scale_and_does_not_infer_rating_count():
    later_at = RECEIVED + timedelta(days=1)
    history = ObservationHistory(
        ticker="1234",
        cutoff=later_at,
        analyst_ratings=(
            AnalystRatingHistory(
                previous=rating(rating_count=None),
                latest=rating(later_at, rating=4, rating_scale_direction=None, rating_count=None),
            ),
        ),
    )
    result = analyze(history, evaluated_at=later_at)
    assert result.analyst_ratings[0].comparison.reason == "unknown_rating_scale_direction"
    assert result.analyst_ratings[0].comparison.latest_count is None
    assert result.analyst_ratings[0].comparison.delta is None


@pytest.mark.parametrize(("previous_count", "latest_count"), [(4, None), (None, 4)])
def test_one_unknown_count_blocks_same_population_comparison(previous_count, latest_count):
    later_at = RECEIVED + timedelta(days=1)
    history = ObservationHistory(
        ticker="1234",
        cutoff=later_at,
        model_valuations=(
            ModelValuationHistory(
                previous=valuation(consensus_target_count=previous_count),
                latest=valuation(
                    later_at, consensus_target=1300, consensus_target_count=latest_count
                ),
            ),
        ),
        analyst_ratings=(
            AnalystRatingHistory(
                previous=rating(rating_count=previous_count),
                latest=rating(later_at, rating=4, rating_count=latest_count),
            ),
        ),
    )
    result = analyze(history, evaluated_at=later_at)
    for metric in (result.model_valuations[0].comparisons[0], result.analyst_ratings[0].comparison):
        assert metric.state == "not_comparable"
        assert metric.reason == "unknown_count"
        assert not metric.comparable
        assert metric.delta is None
        assert metric.latest_count == latest_count
        assert metric.previous_count == previous_count


def test_both_unknown_counts_keep_existing_basis_comparison():
    later_at = RECEIVED + timedelta(days=1)
    result = comparison(
        valuation(consensus_target_count=None),
        valuation(later_at, consensus_target=1300, consensus_target_count=None),
    )
    assert result.model_valuations[0].comparisons[0].comparable
    assert result.model_valuations[0].comparisons[0].delta == 100


def test_models_and_analysis_reject_unknown_time_or_future_input():
    with pytest.raises(ValidationError, match="timezone-aware"):
        valuation(RECEIVED.replace(tzinfo=None))
    with pytest.raises(ValidationError, match="JST received date"):
        valuation(snapshot_date="2026-10-10")
    with pytest.raises(ValidationError, match="timezone-aware"):
        rating(rating_updated_at_utc=RECEIVED.replace(tzinfo=None))
    with pytest.raises(ValidationError):
        rating(rating=6)
    with pytest.raises(ValidationError):
        valuation(consensus_target=float("nan"))
    with pytest.raises(ValidationError):
        rating(rating_count=True)
    history = ObservationHistory(
        ticker="1234",
        cutoff=RECEIVED,
        model_valuations=(ModelValuationHistory(latest=valuation(RECEIVED + timedelta(hours=1))),),
    )
    with pytest.raises(ValueError, match="outside ticker/cutoff"):
        analyze(history, evaluated_at=RECEIVED)
    with pytest.raises(ValueError, match="timezone-aware"):
        read_observations(Path("unused.sqlite"), "1234", RECEIVED.replace(tzinfo=None))


def test_leaf_contract_import_does_not_import_io_services():
    code = """
import sys
from baibai_engine.external_assessment.observations import contract, models
for name in (
    'baibai_engine.external_assessment.store',
    'baibai_engine.external_assessment.reader',
    'baibai_engine.external_assessment.acquisition.collection',
    'baibai_engine.storage.observations.schema',
    'baibai_engine.storage.lake.reader',
):
    assert name not in sys.modules, name
assert len(contract.MODEL_VALUATION_COLUMNS) == len(models.ModelValuationObservation.model_fields)
assert len(contract.ANALYST_RATING_COLUMNS) == len(models.AnalystRatingObservation.model_fields)
"""
    subprocess.run([sys.executable, "-c", code], check=True, env=os.environ.copy())
