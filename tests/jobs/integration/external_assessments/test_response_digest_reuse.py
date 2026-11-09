"""同じbundleのdigestは共有し、別の保存・読戻し境界の照合は保つ。"""

from datetime import timedelta

import pytest
from tests.helpers.external_assessment_publication import (
    RECEIVED,
    _stage,
    pair_responses,
)
from tests.helpers.lake_release import market_store
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.machine_analysis.observations.external_assessments.normalization.decode import (
    decode_attempt,
)
from baibai_engine.storage.sqlite.source_cache.equity_web_import import import_responses
from baibai_jobs.acquisition.external_assessments.cohorts import make_plan, new_result
from baibai_jobs.acquisition.external_assessments.staging import CohortStage, read_stages
from baibai_jobs.integration import external_assessments as integration
from baibai_jobs.publication.public_page_verification import response_digest


@pytest.fixture
def originals(tmp_path):
    store = MemoryR2Store()
    plan = make_plan(
        run_id="digest-reuse",
        tickers=("6758", "7203"),
        sources=("monex", "rakuten", "kabumap", "minkabu"),
        cohort_size=1,
        universe_binding="fixed-test-universe",
        collector_binding="fixed-test-collector",
    )
    bundles, observations = [], []
    for cohort in plan.cohorts:
        entries, responses = [], []
        for source, ticker in cohort.pairs:
            pair = pair_responses(
                source,
                ticker,
                received_at_utc=RECEIVED + timedelta(days=cohort.index),
                value="--" if ticker == "6758" else None,
            )
            observation = decode_attempt(pair)
            responses.extend(pair)
            observations.append(observation)
            entries.append((source, ticker, "observed", pair[0].attempt_id, observation, 200))
        _stage(store, plan, cohort.index, entries, responses)
        bundles.append(tuple(responses))
    stages = read_stages(store, plan)
    expected = {
        stages[index][0].raw_bundle_keys[0]: {
            item.receipt_id: response_digest(item) for item in responses
        }
        for index, responses in enumerate(bundles)
    }
    return (
        store,
        plan,
        stages,
        bundles,
        tuple(observations),
        expected,
        market_store(tmp_path / "market.sqlite"),
    )


@pytest.mark.parametrize("acknowledged", [False, True])
def test_import_shares_each_bundle_digest_without_changing_receipts(
    originals, monkeypatch, acknowledged
):
    store, plan, stages, bundles, observations, expected, database = originals
    if acknowledged:
        for responses in bundles:
            import_responses(database, responses)
        store.values.clear()  # 既公開の原本はhydrate済みDBから読む。
    calls = []

    def counted(response):
        calls.append(response.receipt_id)
        return response_digest(response)

    monkeypatch.setattr(integration, "equity_web_response_digest", counted)
    retained, bundle_receipts = (set(), {}), {}
    actual = integration._import_confirmed_originals(
        store,
        plan,
        stages,
        database,
        retained=retained,
        acknowledged=expected if acknowledged else None,
        bundle_receipts=bundle_receipts,
    )
    digests = {
        identity: digest for values in expected.values() for identity, digest in values.items()
    }
    months = {(2026, 9), (2026, 10)}
    assert actual == (0 if acknowledged else len(digests), months, digests, observations)
    assert retained == (months, digests)
    assert bundle_receipts == expected
    assert sorted(calls) == sorted(digests)


def test_acknowledged_digest_mismatch_still_stops_before_import(originals):
    store, plan, stages, bundles, _, expected, database = originals
    for responses in bundles:
        import_responses(database, responses)
    first = next(iter(expected.values()))
    first[next(iter(first))] = "0" * 64
    before = database.read_bytes()
    retained, bundle_receipts = (set(), {}), {}
    with pytest.raises(ValueError, match="acknowledged external original changed"):
        integration._import_confirmed_originals(
            store,
            plan,
            stages,
            database,
            retained=retained,
            acknowledged=expected,
            bundle_receipts=bundle_receipts,
        )
    assert database.read_bytes() == before
    assert retained == (set(), {})
    assert bundle_receipts == {}


def test_empty_stage_needs_no_digest(originals, monkeypatch):
    store, plan, _, _, _, _, database = originals

    def unexpected(_):
        pytest.fail("an unattempted stage has no original to digest")

    monkeypatch.setattr(integration, "equity_web_response_digest", unexpected)
    assert integration._import_confirmed_originals(
        store, plan, {0: (CohortStage(new_result(plan, 0)), "unused")}, database
    ) == (0, set(), {}, ())
