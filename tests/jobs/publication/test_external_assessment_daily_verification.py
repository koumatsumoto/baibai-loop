from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import timedelta

from tests.helpers.external_assessment_universe import (
    RECEIVED,
    seed_database,
)

from baibai_engine.machine_analysis.observations.external_assessments.contract import (
    ANALYST_FORECAST_TABLE,
    ANALYST_RATING_TABLE,
    MODEL_VALUATION_TABLE,
    QUANTS_SCORE_TABLE,
)
from baibai_engine.machine_analysis.observations.external_assessments.models import (
    AnalystRatingObservation,
    ModelValuationObservation,
)
from baibai_engine.machine_analysis.observations.external_assessments.reader import (
    read_observations,
)
from baibai_engine.machine_analysis.observations.external_assessments.store import save_observations
from baibai_jobs.publication.external_assessment_verification import (
    history_digests,
)


def model(at=RECEIVED, **kwargs):
    return ModelValuationObservation.model_validate(
        {
            "ticker": "7203",
            "fetched_at_utc": at,
            "distributor": "monex",
            "origin": "synthetic-distributor",
            "source_url": "https://synthetic.invalid/model",
            "parser_version": "synthetic-1",
            "consensus_target": 100,
            "per_company_forecast": 101,
            "per_consensus": 102,
            "pbr": 103,
            "currency": "JPY",
            "unit": "JPY/share",
            "share_basis": "synthetic-common",
            "per_consensus_origin": "synthetic-origin",
            "per_consensus_period": "synthetic-FY",
            "per_consensus_count": 4,
            "per_consensus_updated_label": "synthetic-update",
            **kwargs,
        }
    )


def rating(at=RECEIVED, **kwargs):
    return AnalystRatingObservation.model_validate(
        {
            "ticker": "7203",
            "fetched_at_utc": at,
            "distributor": "rakuten",
            "origin": "synthetic-origin",
            "source_url": "https://synthetic.invalid/rating",
            "parser_version": "synthetic-1",
            "rating": 3.5,
            "rating_scale_min": 1,
            "rating_scale_max": 5,
            "rating_scale_direction": "higher_is_positive",
            "rating_count": 4,
            "rating_updated_label": "synthetic-update",
            **kwargs,
        }
    )


def other_dataset_digest(path):
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        rows = {
            table: connection.execute(f'SELECT * FROM "{table}" ORDER BY 1,2').fetchall()
            for table in ("jquants_master_snapshots", "source_coverage", "lake_store_origin")
        }
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def test_actual_receipt_readback_preserves_latest_null_and_both_previous_histories(tmp_path):
    database = tmp_path / "market.sqlite"
    seed_database(database, count=1)
    old_model, old_rating = model(), rating()
    latest_at = RECEIVED + timedelta(hours=1)
    missing_model = model(
        latest_at, consensus_target=None, per_company_forecast=None, per_consensus=None, pbr=None
    )
    missing_rating = rating(latest_at, rating=None)
    assert save_observations(database, [old_model, old_rating, missing_model, missing_rating]) == 4
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    history = read_observations(database, "7203", latest_at)
    assert history.model_valuations[0].latest == missing_model
    assert history.model_valuations[0].previous == old_model
    assert history.analyst_ratings[0].latest == missing_rating
    assert history.analyst_ratings[0].previous == old_rating
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_history_digests_cover_all_tables_and_old_rows_without_mutating_other_tables(tmp_path):
    database = tmp_path / "market.sqlite"
    seed_database(database, count=1)
    other_before = other_dataset_digest(database)
    empty = history_digests(database)
    assert set(empty) == {
        MODEL_VALUATION_TABLE,
        ANALYST_RATING_TABLE,
        QUANTS_SCORE_TABLE,
        ANALYST_FORECAST_TABLE,
    }
    assert set(empty.values()) == {hashlib.sha256(b"").hexdigest()}
    save_observations(database, [model(), rating()])
    initial = history_digests(database)
    save_observations(database, [model(RECEIVED + timedelta(hours=1))])
    later = history_digests(database)
    assert later[MODEL_VALUATION_TABLE] != initial[MODEL_VALUATION_TABLE]
    assert later[ANALYST_RATING_TABLE] == initial[ANALYST_RATING_TABLE]
    with closing(sqlite3.connect(database)) as connection, connection:
        updated = connection.execute(
            "UPDATE external_assessment_model_valuation_observations SET per_consensus_origin='changed-old-row' WHERE fetched_at_utc=?",
            (RECEIVED.isoformat(timespec="microseconds"),),
        )
        assert updated.rowcount == 1
    changed_old = history_digests(database)
    assert changed_old[MODEL_VALUATION_TABLE] != later[MODEL_VALUATION_TABLE]
    assert changed_old[ANALYST_RATING_TABLE] == later[ANALYST_RATING_TABLE]
    save_observations(database, [rating(RECEIVED + timedelta(hours=1))])
    changed_rating = history_digests(database)
    assert changed_rating[MODEL_VALUATION_TABLE] == changed_old[MODEL_VALUATION_TABLE]
    assert changed_rating[ANALYST_RATING_TABLE] != changed_old[ANALYST_RATING_TABLE]
    assert other_dataset_digest(database) == other_before
    before_read = hashlib.sha256(database.read_bytes()).hexdigest()
    assert history_digests(database) == changed_rating
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_read
