"""Public HTML attempts with canonical IDs for fixed-release publication tests."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from tests.engine.test_external_assessment_sources import monex_html, official_html, rakuten_html
from tests.engine.test_kabumap import html as kabumap_html
from tests.engine.test_minkabu import html as minkabu_html
from tests.helpers.l1_release import market_store
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.master import insert_master_rows
from tests.helpers.r2_store import MemoryR2Store
from tests.jobs.external_assessment_response_fixtures import response

from baibai_engine.data_collection.equity_web.models import EquityWebSourceResponse, Source
from baibai_engine.job_api import (
    ExternalAssessmentAnalystRatingObservation,
    ExternalAssessmentCollectionOutcome,
    ExternalAssessmentModelValuationObservation,
    external_assessment_collector_binding,
    external_assessment_decode_attempt,
    external_assessment_save_observations,
)
from baibai_engine.machine_analysis.external_assessment.normalization import (
    kabumap,
    minkabu,
    monex,
    rakuten,
)
from baibai_jobs.jobs import external_assessment_integration as external_assessment
from baibai_jobs.jobs.external_assessment_cohorts import (
    RunPlan,
    make_plan,
    new_result,
    record_outcome,
)
from baibai_jobs.storage import external_assessment_staging as staging
from baibai_jobs.storage import publish_market_lake as publisher
from baibai_jobs.storage.external_assessment_raw import RawBundle
from baibai_jobs.storage.external_assessment_ready import prepare_plan, save_ready
from baibai_jobs.storage.external_assessment_staging import (
    CohortStage,
    put_private,
    stage_bytes,
    stage_key,
)
from baibai_jobs.storage.lake_publish import ObjectStore
from baibai_jobs.storage.publication_lease import acquire, verify_held


def pair_responses(
    source: Source,
    ticker: str,
    *,
    received_at_utc: datetime,
    value: str | None = None,
    malformed: bool = False,
) -> tuple[EquityWebSourceResponse, ...]:
    if source == "monex":
        body = (
            "<html>invalid public page</html>"
            if malformed
            else monex_html(ticker, value=value or "1,234円")
        )
        return (
            response(
                source,
                ticker,
                body.encode(),
                url=monex.public_url(ticker),
                received_at_utc=received_at_utc,
            ),
        )
    if source == "minkabu":
        body = "<html>invalid public page</html>" if malformed else minkabu_html(ticker)
        return (
            response(
                source,
                ticker,
                body.encode(),
                url=minkabu.public_url(ticker),
                received_at_utc=received_at_utc,
            ),
        )
    if source == "kabumap":
        body = "<html>invalid public page</html>" if malformed else kabumap_html(ticker)
        return (
            response(
                source,
                ticker,
                body.encode("shift_jis"),
                url=kabumap.public_url(ticker),
                received_at_utc=received_at_utc,
                encoding="shift_jis",
            ),
        )
    outer = official_html(ticker)
    page = response(
        source,
        ticker,
        outer.encode(),
        url=rakuten.public_url(ticker),
        received_at_utc=received_at_utc,
    )
    iframe = response(
        source,
        ticker,
        (
            "<html>unconfirmed rating page</html>"
            if malformed
            else rakuten_html(ticker, value=value or "3.25")
        ).encode(),
        attempt_id=page.attempt_id,
        role="iframe",
        parent_receipt_id=page.receipt_id,
        url=rakuten.iframe_url(outer, ticker=ticker, source_url=page.requested_url),
        received_at_utc=received_at_utc,
    )
    return page, iframe


def upload_bundle(
    store: ObjectStore,
    plan: RunPlan,
    index: int,
    responses: tuple[EquityWebSourceResponse, ...],
) -> str:
    with RawBundle(plan.run_id, index) as bundle:
        for item in responses:
            bundle.append(item)
        key = bundle.finish()
        assert key is not None
        bundle.upload(store)
    return key


RECEIVED = datetime(2026, 9, 30, 15, tzinfo=UTC)


@pytest.fixture
def publication(tmp_path, monkeypatch):
    datasets = (
        "jquants.master_snapshots",
        "jquants.daily_bars",
        "jquants.short_sale_reports",
        external_assessment.SOURCE_RESPONSES_DATASET,
        external_assessment.VALUATION_DATASET,
        external_assessment.RATING_DATASET,
        external_assessment.QUANTS_DATASET,
        external_assessment.ANALYST_FORECAST_DATASET,
        "tradingview.forecast_snapshots",
    )
    narrow_release_policy(monkeypatch, datasets=datasets)
    monkeypatch.setattr(publisher, "lake_verified_git_commit", lambda: "a" * 40)
    database = market_store(tmp_path / "market.sqlite")
    with closing(sqlite3.connect(database)) as connection, connection:
        insert_master_rows(
            connection, [("2026-09-30", "7203", "合成会社", "PRIME", "輸送用機器", "011")]
        )
    common = {
        "ticker": "7203",
        "fetched_at_utc": RECEIVED - timedelta(days=1),
        "source_url": "https://example.test/synthetic",
        "parser_version": "synthetic-1",
    }
    external_assessment_save_observations(
        database,
        [
            ExternalAssessmentModelValuationObservation(
                **common, distributor="monex", consensus_target=91
            ),
            ExternalAssessmentAnalystRatingObservation(**common, distributor="rakuten", rating=2),
            ExternalAssessmentModelValuationObservation(
                ticker="6758",
                fetched_at_utc=datetime(2026, 7, 31, 15, tzinfo=UTC),
                source_url="https://example.test/synthetic",
                parser_version="synthetic-1",
                distributor="monex",
                consensus_target=0,
                consensus_target_origin="synthetic-origin",
                consensus_target_count=0,
            ),
            ExternalAssessmentAnalystRatingObservation(
                ticker="6758",
                fetched_at_utc=datetime(2026, 7, 31, 15, tzinfo=UTC),
                source_url="https://example.test/synthetic",
                parser_version="synthetic-1",
                distributor="rakuten",
                rating=None,
                rating_updated_label="synthetic-label",
            ),
        ],
    )
    store = MemoryR2Store()
    mirror = tmp_path / "mirror"
    publisher.publish_market_lake(
        sqlite_path=database,
        mirror_root=mirror,
        store=store,
        release_id="before-retail-daily",
    )
    handle, _ = acquire(store, "operator")
    plan = make_plan(
        sources=("monex",),
        run_id="monex-synthetic-daily",
        tickers=("7203", "6758"),
        universe_binding="synthetic-fixed-master",
        collector_binding=external_assessment_collector_binding(),
    )
    journal = new_result(plan, 0)
    originals = []
    for source, ticker in plan.cohorts[0].pairs:
        responses = pair_responses(
            source,
            ticker,
            received_at_utc=RECEIVED,
            value=("123,456円" if source == "monex" and ticker == "7203" else "--")
            if source == "monex"
            else "3.25",
            malformed=(source == "rakuten" and ticker == "6758"),
        )
        originals.extend(responses)
        observation = (
            None
            if source == "rakuten" and ticker == "6758"
            else external_assessment_decode_attempt(responses)
        )
        outcome = ExternalAssessmentCollectionOutcome(
            source,
            ticker,
            "observed" if observation else "parse_failure",
            None if observation else "no_estimates_identity_unconfirmed",
            200,
            responses[0].attempt_id,
        )
        journal = record_outcome(plan, journal, outcome, observation)
    bundle_key = upload_bundle(store, plan, 0, tuple(originals))
    put_private(
        store,
        stage_key(plan, 0),
        stage_bytes(plan, CohortStage(journal, raw_bundle_keys=(bundle_key,))),
        previous_etag=None,
    )
    return store, plan, handle, database, mirror, tmp_path


@pytest.fixture
def independent_plans(publication):
    store, _original, handle, database, mirror, directory = publication
    selected = []
    for source in ("monex", "rakuten"):
        p = make_plan(
            run_id=f"{source}-independent",
            sources=(source,),
            tickers=("6758", "7203"),
            universe_binding=json.dumps({"master_asof": "2026-10-09"}),
            collector_binding=external_assessment_collector_binding(source=source),
        )
        journal = new_result(p, 0)
        originals = []
        for _, ticker in p.cohorts[0].pairs:
            responses = pair_responses(
                source, ticker, received_at_utc=RECEIVED + timedelta(seconds=1)
            )
            originals.extend(responses)
            observation = external_assessment_decode_attempt(responses)
            journal = record_outcome(
                p,
                journal,
                ExternalAssessmentCollectionOutcome(
                    source, ticker, "observed", None, 200, responses[0].attempt_id
                ),
                observation,
            )
        key = upload_bundle(store, p, 0, tuple(originals))
        staging.save_new_plan(store, p)
        store.store_object(
            staging.stage_key(p, 0),
            staging.stage_bytes(p, staging.CohortStage(journal, raw_bundle_keys=(key,))),
        )
        selected.append({"source": source, "run_id": p.run_id})
    return store, selected, handle, database, mirror, directory


def _plan(previous, *, run_id, tickers=("7203",), sources=("monex",), cohort_size=50):
    return make_plan(
        run_id=run_id,
        tickers=tickers,
        sources=sources,
        cohort_size=cohort_size,
        universe_binding=previous.universe_binding,
        collector_binding=external_assessment_collector_binding(
            source=sources[0] if len(sources) == 1 else None
        ),
    )


def _stage(store, plan, index, entries, responses=()):
    journal = new_result(plan, index)
    for source, ticker, status, attempt_id, observation, http_status in entries:
        journal = record_outcome(
            plan,
            journal,
            ExternalAssessmentCollectionOutcome(
                source,
                ticker,
                status,
                None if status == "observed" else "missing_theory_table",
                http_status,
                attempt_id,
            ),
            observation,
        )
    keys = (upload_bundle(store, plan, index, tuple(responses)),) if responses else ()
    store.store_object(
        stage_key(plan, index), stage_bytes(plan, CohortStage(journal, raw_bundle_keys=keys))
    )


def ready_reference(store, plan, *, confirmed_partial=False):
    """Freeze the same exact source plan that the acquisition workflow hands off."""
    if staging.read_plan(store, plan.run_id) is None:
        staging.save_new_plan(store, plan)
    return save_ready(store, prepare_plan(store, plan, confirmed_partial=confirmed_partial))


def integrate_ready_plans(batch, plans, suffix, *, confirmed_partial=False):
    """Exercise merge, the shared publisher and readback without a source-only publisher."""
    store, _, handle, database, mirror, directory = batch
    references = [
        ready_reference(store, plan, confirmed_partial=confirmed_partial) for plan in plans
    ]
    state = external_assessment.merge_ready(store, references, database=database)
    assert state["failed"] == []
    verify_held(store, handle)
    publisher.publish_market_lake(
        sqlite_path=database,
        mirror_root=mirror,
        store=store,
        before_pointer_switch=lambda: verify_held(store, handle),
    )
    return external_assessment.verify_and_seal(
        store, state, handle=handle, directory=directory / suffix
    )
