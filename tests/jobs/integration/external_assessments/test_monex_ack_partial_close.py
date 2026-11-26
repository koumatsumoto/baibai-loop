"""Explicit Monex close prefers raw and can recover only its own acknowledged receipt."""

import json
from contextlib import closing
from gzip import BadGzipFile
from shutil import copyfile
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from tests.helpers.external_assessment_html import monex_eps_section, monex_html
from tests.helpers.external_assessment_publication import (
    RECEIVED,
    _plan,
    _stage,
    pair_responses,
    prepare_plan,
)
from tests.helpers.external_assessment_publication import publication as publication
from tests.helpers.external_assessment_responses import response
from tests.helpers.model_generation import generation, publish_generation

from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
from baibai_engine.machine_analysis.generation.query import read_generation
from baibai_engine.machine_analysis.models.external_assessment.analysis import analyze
from baibai_engine.machine_analysis.observations.external_assessments.history import (
    MonexGenerationWindow,
    latest_monex,
)
from baibai_engine.machine_analysis.observations.external_assessments.monex_generation import (
    read_generation_monex_eps,
)
from baibai_engine.machine_analysis.observations.external_assessments.normalization import monex
from baibai_engine.machine_analysis.observations.external_assessments.normalization.decode import (
    decode_attempt,
)
from baibai_engine.machine_analysis.observations.external_assessments.reader import (
    read_observation_population,
)
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.hydrate import hydrate_market_store
from baibai_engine.storage.snapshots.objects import LakeObjectCache
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.external_assessments import staging
from baibai_jobs.integration.external_assessment_ready import (
    acknowledgement_key,
    available_ready,
    save_ready,
)
from baibai_jobs.integration.external_assessments import merge_ready, verify_and_seal
from baibai_jobs.publication import snapshot_publication as publisher

_TICKER = "6758"


def _publish(publication, reference, *, database=None):
    store, _, handle, initial, mirror, directory = publication
    database = initial if database is None else database
    state = merge_ready(store, [reference], database=database)
    assert state["failed"] == []
    publisher.publish_market_lake(sqlite_path=database, mirror_root=mirror, store=store)
    (result,) = verify_and_seal(store, state, handle=handle, directory=directory / "readback")
    assert result["readback"] == "verified"
    return result


def _prepare(publication, run_id, *, source="monex", partial=True, cumulative=True, originals=None):
    store, previous, *_ = publication
    tickers = (_TICKER, "7203") if partial else (_TICKER,)
    plan = _plan(previous, run_id=run_id, sources=(source,), tickers=tickers, cohort_size=1)
    staging.save_new_plan(store, plan)
    if originals is None:
        originals = pair_responses(source, _TICKER, received_at_utc=RECEIVED)
    _stage(
        store,
        plan,
        0,
        [(source, _TICKER, "observed", originals[0].attempt_id, decode_attempt(originals), 200)],
        originals,
    )
    ref = save_ready(store, prepare_plan(store, plan, cumulative=cumulative))
    return plan, ref, _publish(publication, ref)


def _hydrate(publication):
    store, *_, directory = publication
    mirror = directory / "fresh-mirror"
    release = publisher._resolve_serving_release(
        store, mirror, publisher._serving_pointer(store).pointer
    )
    database = directory / "fresh.sqlite"
    open_connection(database).close()
    with lake_session() as session:
        hydrate_market_store(
            session,
            release=release,
            cache=LakeObjectCache(root=mirror, source=SimpleNamespace(read_bytes=store.get_bytes)),
            store=database,
            dataset_names=release.dataset_names(),
            catalog=publisher.LAKE_DATASETS,
        )
    return database


def _valuation(database):
    window = MonexGenerationWindow(
        as_of=RECEIVED.astimezone(ZoneInfo("Asia/Tokyo")).date(), received_through=RECEIVED
    )
    history = read_observation_population(
        database, tickers=(_TICKER,), cutoff=RECEIVED, monex_window=window
    )[_TICKER]
    price = latest_monex(history.model_valuations)
    assert price is not None
    with closing(open_connection(database)) as conn:
        eps = read_generation_monex_eps(
            conn, latest={_TICKER: price}, cutoff=RECEIVED, as_of=window.as_of
        )
    return history, price, eps


def test_partial_close_after_identical_html_reuses_only_exact_ack(publication):
    store, _, _, initial, _, directory = publication
    body = (monex_html(_TICKER) + monex_eps_section()).encode()

    def receipt():
        return response(
            "monex", _TICKER, body, url=monex.public_url(_TICKER), received_at_utc=RECEIVED
        )

    a, b = receipt(), receipt()
    plan, first, initial_report = _prepare(publication, "earlier-a", originals=(a,))
    assert initial_report["state_counts"]["unattempted"] == 1
    assert staging.read_terminal(store, plan) is None
    ack_key = acknowledgement_key(plan, first)
    original_ack = store.values[ack_key].body
    history, price, eps = _valuation(initial)
    assert price == decode_attempt((a,))
    assert eps[_TICKER].receipt_id == a.receipt_id
    assert eps[_TICKER].reference_eps.current_fy.eps_jpy_per_share == -3.5
    model = analyze(history, evaluated_at=RECEIVED, monex_fiscal_eps=eps[_TICKER])
    payload = model.model_dump(mode="json")
    generated = GeneratedModel(
        model_id="external_assessment",
        subject=_TICKER,
        calculation_revision=model.method_version,
        payload=payload,
    )
    issued = generation().model_copy(
        update={"as_of": price.snapshot_date, "evaluation_time": RECEIVED, "models": (generated,)}
    )
    ledger = directory / "runs.sqlite"
    assert publish_generation(issued, path=ledger)

    assert a.receipt_id != b.receipt_id
    assert a.attempt_id != b.attempt_id
    assert a.body == b.body
    assert decode_attempt((b,)) == price
    _, _, duplicate_report = _prepare(
        publication, "independent-b", partial=False, cumulative=False, originals=(b,)
    )
    assert duplicate_report["rows_added"] == 0
    assert duplicate_report["source_responses_added"] == 1
    database = _hydrate(publication)
    assert _valuation(database)[1:] == (price, {})
    (raw,) = staging.read_stage(store, plan, 0)[0].raw_bundle_keys
    store.delete_object(raw)
    close = save_ready(store, prepare_plan(store, plan, confirmed_partial=True))
    assert available_ready(store) == ([close], [])

    # B matches time, URL and HTML, not receipt identity.
    without_a = directory / "independent-b-only.sqlite"
    copyfile(database, without_a)
    with closing(open_connection(without_a)) as conn, conn:
        (removed,) = conn.execute(
            "DELETE FROM equity_web_source_responses WHERE receipt_id=? RETURNING receipt_id",
            (a.receipt_id,),
        ).fetchall()
    assert removed == (a.receipt_id,)
    rejected = merge_ready(store, [close], database=without_a)
    assert rejected["plans"] == []
    assert rejected["failed"] == [{"reference": close, "reason": "EquityWebReceiptError"}]
    assert staging.read_terminal(store, plan) is None

    store.get_keys.clear()
    store.head_keys.clear()
    result = _publish(publication, close, database=database)
    assert result["state_counts"] == {
        "failure": 0,
        "success": 1,
        "normal_null": 0,
        "unattempted": 1,
    }
    assert result["source_responses_added"] == result["rows_added"] == 0
    assert set(result["bundle_receipts"][raw]) == {a.receipt_id}
    assert (
        store.get_keys.count(raw) == store.head_keys.count(raw) == 1
    )  # Confirm absence, not a body read.
    assert store.values[ack_key].body == original_ack
    assert merge_ready(store, [close], database=database)["plans"] == []
    assert available_ready(store) == ([], [])
    assert store.put_keys.count(staging.prefix(plan.run_id) + "/published.json") == 1
    assert _valuation(database)[1:] == (price, {})
    assert (
        read_generation(issued.generation_id, path=ledger)
        .result("external_assessment", _TICKER)
        .payload
        == payload
    )


@pytest.mark.parametrize(
    ("raw_state", "ack_state", "canonical_state", "result"),
    [
        ("present", "valid", "absent", "verified"),
        ("present", "invalid", "healthy", "verified"),
        ("corrupt", "valid", "healthy", "incomplete"),
        ("missing", "valid", "changed", "incomplete"),
        ("missing", "absent", "healthy", "incomplete"),
        ("missing", "invalid", "healthy", "incomplete"),
        ("missing", "noncumulative", "healthy", "incomplete"),
        ("missing", "valid", "corrupt", "fatal"),
    ],
)
def test_partial_close_raw_and_ack_boundaries(
    publication, raw_state, ack_state, canonical_state, result
):
    store, _, _, database, *_ = publication
    plan, first, report = _prepare(publication, "raw-first-boundaries")
    close = save_ready(store, prepare_plan(store, plan, confirmed_partial=True))
    ((raw, identities),) = report["bundle_receipts"].items()
    (receipt_id,) = identities
    ack_key = acknowledgement_key(plan, first)
    if raw_state == "missing":
        store.delete_object(raw)
    elif raw_state == "corrupt":
        store.store_object(raw, b"invalid gzip", content_type="application/gzip")
    if ack_state == "invalid":
        store.store_object(ack_key, b"[]")
    elif ack_state == "absent":
        store.delete_object(ack_key)
    elif ack_state == "noncumulative":
        value = json.loads(store.values[ack_key].body)
        value["reference"] = close
        store.delete_object(ack_key)
        store.store_object(acknowledgement_key(plan, close), json.dumps(value).encode())
    with closing(open_connection(database)) as conn, conn:
        if canonical_state == "absent":
            conn.execute(
                "DELETE FROM equity_web_source_responses WHERE receipt_id=?", (receipt_id,)
            )
        elif canonical_state == "changed":
            conn.execute(
                "UPDATE equity_web_source_responses SET selected_encoding='UTF-8' WHERE receipt_id=?",
                (receipt_id,),
            )
        elif canonical_state == "corrupt":
            conn.execute(
                "UPDATE equity_web_source_responses SET body_gzip=x'7878' WHERE receipt_id=?",
                (receipt_id,),
            )
    store.get_keys.clear()
    terminal_key = staging.prefix(plan.run_id) + "/published.json"
    if result == "verified":
        assert _publish(publication, close)["source_responses_added"] == int(
            canonical_state == "absent"
        )
        assert terminal_key in store.put_keys
    else:
        if result == "fatal":
            with pytest.raises(BadGzipFile):
                merge_ready(store, [close], database=database)
        else:
            failed = merge_ready(store, [close], database=database)
            assert failed["plans"] == []
            assert [item["reference"] for item in failed["failed"]] == [close]
        assert terminal_key not in store.put_keys
    if raw_state != "missing":
        assert ack_key not in store.get_keys


@pytest.mark.parametrize(("source", "partial"), [("monex", False), ("kabumap", True)])
def test_absent_raw_ack_reuse_is_specific_to_monex_explicit_partial(publication, source, partial):
    store, _, _, database, *_ = publication
    plan, first, report = _prepare(
        publication, "excluded-missing-raw", source=source, partial=partial
    )
    (raw,) = report["bundle_receipts"]
    store.delete_object(raw)
    close = save_ready(store, prepare_plan(store, plan, confirmed_partial=partial))
    store.get_keys.clear()
    state = merge_ready(store, [close], database=database)
    assert state["plans"] == []
    assert state["failed"] == [{"reference": close, "reason": "ValueError"}]
    assert acknowledgement_key(plan, first) not in store.get_keys
    assert staging.read_terminal(store, plan) is None
