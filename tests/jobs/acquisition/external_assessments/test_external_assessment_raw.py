"""Canonical raw bundles are durable prerequisites of typed cohort checkpoints."""

import gzip
import io
import json
import tarfile
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from tests.helpers.external_assessment_html import (
    monex_html,
    official_html,
    rakuten_html,
)
from tests.helpers.external_assessment_http import RawSession as Session
from tests.helpers.external_assessment_http import connect as connect
from tests.helpers.external_assessment_publication import pair_responses, upload_bundle
from tests.helpers.external_assessment_responses import response
from tests.helpers.external_assessment_stages import RECEIVED, FakeObjectStore, plan, stage

import baibai_jobs.acquisition.external_assessments.collection as collection
import baibai_jobs.acquisition.external_assessments.daily as daily
import baibai_jobs.acquisition.external_assessments.raw as external_assessment_raw
import baibai_jobs.acquisition.external_assessments.staging as staging
from baibai_engine.machine_analysis.observations.external_assessments.normalization import (
    ParseError,
    monex,
)
from baibai_engine.machine_analysis.observations.external_assessments.normalization.decode import (
    decode_attempt,
)
from baibai_engine.machine_analysis.observations.external_assessments.store import save_observations
from baibai_engine.storage.objects.r2 import (
    Boto3R2Store,
    LakePublishError,
    R2Error,
)


def bundles(store):
    return [
        key
        for key in store.objects
        if key.startswith(external_assessment_raw.RETAINED_RAW_PREFIX) and "/bundles/" in key
    ]


def stage_or_none(store, plan):
    return staging.read_stage(store, plan, 0)


def test_exact_bytes_encoding_reparse_and_typed_idempotency(tmp_path, connect):
    body = (monex_html() + "<!-- 日本語の元本文 -->").encode("cp932")
    session = Session([(200, body, "cp932")])
    connect(session)
    p, store = plan(), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    saved = stage(store, p)
    metadata, recovered = external_assessment_raw.read_raw_bundle(store, saved.raw_bundle_keys[0])
    assert tuple(item.body for item in recovered) == (body,)
    receipt = metadata["receipts"][0]
    assert receipt["selected_encoding"] == "cp932"
    assert receipt["requested_url"] == monex.public_url("7203")
    assert saved.result.entries[0].attempt_id == recovered[0].attempt_id
    observation = monex.parse_html(
        recovered[0].body.decode(receipt["selected_encoding"]),
        ticker="7203",
        source_url=receipt["requested_url"],
        fetched_at_utc=datetime.fromisoformat(receipt["received_at_utc"]),
    )
    assert saved.result.observations == (observation,)
    path = tmp_path / "observations.sqlite3"
    assert save_observations(path, [observation]) == 1
    assert save_observations(path, [observation]) == 0
    manifest_bytes = json.dumps(metadata).encode()
    assert b"private-cookie-header" not in manifest_bytes
    assert b"private-authorization-header" not in manifest_bytes
    assert [key for key, *_ in store.puts] == [saved.raw_bundle_keys[0], staging.stage_key(p, 0)]


def test_retained_rakuten_outer_and_iframe_bytes_are_in_one_archive():
    p, store = plan(sources=("rakuten",)), FakeObjectStore()
    originals = pair_responses("rakuten", "7203", received_at_utc=RECEIVED)
    key = upload_bundle(store, p, 0, originals)
    manifest, bodies = external_assessment_raw.read_raw_bundle(store, key)
    assert bodies == originals
    assert bodies[0].attempt_id == bodies[1].attempt_id
    assert bodies[1].parent_receipt_id == bodies[0].receipt_id
    assert manifest["receipts"][1]["role"] == "iframe"
    assert decode_attempt(bodies).rating == 3.25
    assert len(store.puts) == 1


@pytest.mark.parametrize("status", [403, 404, 500])
def test_non200_has_no_raw_bundle(connect, status):
    connect(Session([(status, b"denied", "utf-8")]))
    p, store = plan(), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    assert stage(store, p).result.entries[0].state == "failure"
    assert stage(store, p).result.entries[0].attempt_id is not None
    assert stage(store, p).raw_bundle_keys == ()
    assert bundles(store) == []


@pytest.mark.parametrize(
    ("html", "state"), [("invalid public HTML", "failure"), (monex_html(value="--"), "normal_null")]
)
def test_parse_failure_and_normal_null_preserve_original_bytes(connect, html, state):
    connect(Session([(200, html.encode(), "utf-8")]))
    p, store = plan(), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    saved = stage(store, p)
    assert saved.result.entries[0].state == state
    assert tuple(
        item.body
        for item in external_assessment_raw.read_raw_bundle(store, saved.raw_bundle_keys[0])[1]
    ) == (html.encode(),)


def test_decode_error_keeps_exact_received_bytes_before_failed_parse(connect):
    connect(Session([(200, b"\xff source bytes", "utf-8")]))
    p, store = plan(), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    saved = stage(store, p)
    entry = saved.result.entries[0]
    assert (entry.state, entry.reason) == ("failure", "response_encoding")
    _, responses = external_assessment_raw.read_raw_bundle(store, saved.raw_bundle_keys[0])
    assert len(responses) == 1
    assert responses[0].body == b"\xff source bytes"
    assert responses[0].attempt_id == entry.attempt_id


@pytest.mark.parametrize("error_type", [ParseError, ValueError, RuntimeError])
def test_projection_failure_keeps_batch_bodies_outcomes_and_same_plan_resume(
    connect, monkeypatch, error_type
):
    tickers = ("6758", "7203", "9432")
    bodies = [
        monex_html(ticker, value="--" if ticker == "6758" else "1,234円").encode()
        for ticker in tickers
    ]
    session = Session([(200, body, "utf-8") for body in bodies])
    connect(session)
    parse = collection.parse_source_html

    def project(html, **kwargs):
        if kwargs["ticker"] == "7203":
            raise error_type("missing_theory_table")
        return parse(html, **kwargs)

    monkeypatch.setattr(collection, "parse_source_html", project)
    p, store = plan(tickers=tickers), FakeObjectStore()
    report = daily.acquire_cohort(store, p, 0)
    saved = stage(store, p)
    assert report["cohort_complete"] is True
    assert [entry.state for entry in saved.result.entries] == ["normal_null", "failure", "success"]
    reason = "missing_theory_table" if error_type is ParseError else "unclassified_parse_failure"
    assert saved.result.entries[1].reason == reason
    assert tuple(item.ticker for item in saved.result.observations) == ("6758", "9432")
    _, restored = external_assessment_raw.read_raw_bundle(store, saved.raw_bundle_keys[0])
    assert tuple(item.body for item in restored) == tuple(bodies)
    assert tuple(item.attempt_id for item in restored) == tuple(
        entry.attempt_id for entry in saved.result.entries
    )
    assert len(session.calls) == 3
    before = list(store.puts)
    assert daily.acquire_cohort(store, p, 0)["provider_requests"] == 0
    assert len(session.calls) == 3
    assert store.puts == before


def _unexpected_projection(*args, **kwargs):
    raise RuntimeError("private parser detail")


@pytest.mark.parametrize("fault", ["before", "after", "bad_receipt"])
@pytest.mark.parametrize("projection_failure", [False, True])
def test_raw_upload_failure_prevents_stage_commit(
    connect, monkeypatch, capsys, fault, projection_failure
):
    connect(Session([(200, monex_html().encode(), "utf-8")]))
    if projection_failure:
        monkeypatch.setattr(collection, "parse_source_html", _unexpected_projection)
    p, store = plan(), FakeObjectStore()
    store.put_fault = (1, fault)
    with pytest.raises(LakePublishError):
        daily.acquire_cohort(store, p, 0)
    assert stage_or_none(store, p) is None
    assert len(store.puts) == 1
    assert "private secret" not in capsys.readouterr().err


@pytest.mark.parametrize("method", ["append", "finish"])
@pytest.mark.parametrize("projection_failure", [False, True])
def test_local_raw_failure_prevents_stage_commit(
    connect, monkeypatch, capsys, method, projection_failure
):
    connect(Session([(200, monex_html().encode(), "utf-8")]))
    if projection_failure:
        monkeypatch.setattr(collection, "parse_source_html", _unexpected_projection)

    def fail(*a, **k):
        raise OSError("private secret must not be logged")

    monkeypatch.setattr(external_assessment_raw.RawBundle, method, fail)
    p, store = plan(), FakeObjectStore()
    with pytest.raises(OSError, match="private secret must not be logged"):
        daily.acquire_cohort(store, p, 0)
    assert stage_or_none(store, p) is None
    assert store.puts == []
    error = capsys.readouterr().err
    assert "private secret" not in error


def test_projection_failure_raw_survives_unsaved_stage_cas(connect, monkeypatch):
    body = monex_html().encode()
    session = Session([(200, body, "utf-8")])
    connect(session)
    monkeypatch.setattr(collection, "parse_source_html", _unexpected_projection)
    p, store = plan(), FakeObjectStore()
    store.put_fault = (2, "cas")
    with pytest.raises(LakePublishError):
        daily.acquire_cohort(store, p, 0)
    assert stage_or_none(store, p) is None
    assert len(session.calls) == 1
    (key,) = bundles(store)
    assert [path for path, *_ in store.puts] == [key, staging.stage_key(p, 0)]
    candidate = staging.load_stage(p, store.puts[-1][3])
    assert candidate.raw_bundle_keys == (key,)
    entry = candidate.result.entries[0]
    assert (entry.state, entry.reason) == ("failure", "unclassified_parse_failure")
    assert candidate.result.observations == ()
    _, restored = external_assessment_raw.read_raw_bundle(store, candidate.raw_bundle_keys[0])
    assert restored[0].body == body
    assert restored[0].attempt_id == entry.attempt_id


def test_projection_failure_keeps_candidate_raw_when_competing_success_wins(connect, monkeypatch):
    body = monex_html().encode()
    p, winner_store = plan(), FakeObjectStore()
    winner_session = Session([(200, body, "utf-8")])
    connect(winner_session)
    daily.acquire_cohort(winner_store, p, 0)
    winner = stage(winner_store, p)
    winner_key = winner.raw_bundle_keys[0]

    store = FakeObjectStore()
    store.seed(winner_key, winner_store.objects[winner_key][0], "application/gzip")
    session = Session([(200, body, "utf-8")])
    connect(session)
    monkeypatch.setattr(collection, "parse_source_html", _unexpected_projection)
    put = store.put_file

    def conflict(key, path, **kwargs):
        if key == staging.stage_key(p, 0):
            store.seed(key, staging.stage_bytes(p, winner))
        return put(key, path, **kwargs)

    monkeypatch.setattr(store, "put_file", conflict)
    assert daily.acquire_cohort(store, p, 0)["cohort_complete"] is True
    assert stage(store, p) == winner
    (key,) = (key for key in bundles(store) if key != winner_key)
    assert [path for path, *_ in store.puts] == [key, staging.stage_key(p, 0)]
    candidate = staging.load_stage(p, store.puts[-1][3])
    assert candidate.raw_bundle_keys == (key,)
    assert candidate.result.entries[0].state == "failure"
    assert candidate.result.observations == ()
    assert candidate.raw_bundle_keys != winner.raw_bundle_keys
    _, losing = external_assessment_raw.read_raw_bundle(store, candidate.raw_bundle_keys[0])
    _, retained = external_assessment_raw.read_raw_bundle(store, winner_key)
    assert losing[0].body == retained[0].body == body
    assert losing[0].attempt_id != retained[0].attempt_id
    before = list(store.puts)
    assert daily.acquire_cohort(store, p, 0)["provider_requests"] == 0
    assert len(winner_session.calls) == len(session.calls) == 1
    assert store.puts == before


def test_raw_upload_precedes_typed_checkpoint_and_never_downloads_per_body(connect, monkeypatch):
    connect(Session([(200, monex_html().encode(), "utf-8")]))
    p, store = plan(), FakeObjectStore()
    original = store.put_file

    def put(key, path, **kwargs):
        if key.startswith(external_assessment_raw.RETAINED_RAW_PREFIX):
            assert stage_or_none(store, p) is None
        else:
            assert bundles(store)
        return original(key, path, **kwargs)

    monkeypatch.setattr(store, "put_file", put)
    daily.acquire_cohort(store, p, 0)
    assert not any(
        method == "GET" and key.startswith(external_assessment_raw.RETAINED_RAW_PREFIX)
        for method, key in store.events
    )


def test_three_media_normal_and_rakuten_retry_fixture_bundle_sizing_and_restoration():
    from tests.helpers.external_assessment_html import kabumap_html

    pages = [
        monex_html().encode(),
        official_html().encode(),
        rakuten_html().encode(),
        kabumap_html().encode(),
    ]
    store = FakeObjectStore()
    # 50 tickers * (1 Monex + 2 Rakuten + 1 Kabumap + 5 retried Rakuten outer pages).
    expected = []
    with external_assessment_raw.RawBundle("synthetic-sizing", 0) as bundle:
        for i in range(50):
            ticker = str(1000 + i)
            monex_page = response("monex", ticker, pages[0])
            rakuten_attempt = str(uuid.uuid4())
            rakuten_page = response("rakuten", ticker, pages[1], attempt_id=rakuten_attempt)
            rakuten_iframe = response(
                "rakuten",
                ticker,
                pages[2],
                attempt_id=rakuten_attempt,
                role="iframe",
                parent_receipt_id=rakuten_page.receipt_id,
            )
            kabumap_page = response("kabumap", ticker, pages[3])
            for item in (monex_page, rakuten_page, rakuten_iframe, kabumap_page):
                expected.append(item.body)
                bundle.append(item)
            for _ in range(5):
                retry_page = response("rakuten", ticker, pages[1])
                expected.append(retry_page.body)
                bundle.append(retry_page)
        key = bundle.finish()
        bundle.upload(store)
        archive_bytes = bundle.path.stat().st_size
        raw_bytes = sum(map(len, expected))
        assert archive_bytes < raw_bytes
        assert len(store.puts) == 1
        manifest, restored = external_assessment_raw.read_raw_bundle(store, key)
        assert len(manifest["receipts"]) == 450
        assert tuple(item.body for item in restored) == tuple(expected)


def test_authorized_response_is_never_archived_or_treated_as_typed_success(connect):
    class Authenticated(Session):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            response.request.headers["Authorization"] = "private synthetic credential"
            return response

    session = Authenticated([(200, monex_html().encode(), "utf-8")])
    connect(session)
    store = FakeObjectStore()
    with pytest.raises(RuntimeError, match="requires_anonymous_response"):
        daily.acquire_cohort(store, plan(), 0)
    assert store.puts == []
    assert len(session.calls) == 1


def test_bundle_readback_rejects_changed_body_hash_without_extracting_paths(connect):
    import io
    import tarfile

    connect(Session([(200, monex_html().encode(), "utf-8")]))
    p, store = plan(), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    key = stage(store, p).raw_bundle_keys[0]
    with tarfile.open(fileobj=io.BytesIO(store.objects[key][0]), mode="r:gz") as original:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as changed:
            for member in original.getmembers():
                stream = original.extractfile(member)
                body = stream.read()
                if member.name.startswith("bodies/"):
                    body = b"synthetic corruption"
                    member.size = len(body)
                changed.addfile(member, io.BytesIO(body))
    store.seed(key, output.getvalue(), "application/gzip")
    with pytest.raises(LakePublishError, match="body readback mismatch"):
        external_assessment_raw.read_raw_bundle(store, key)


@pytest.mark.parametrize(
    "extra",
    ["empty-gzip-member", "small-gzip-member", "excess-padding", "large-member-with-false-size"],
)
def test_reader_does_not_drain_unreferenced_gzip_expansion(monkeypatch, extra):
    store, p = FakeObjectStore(), plan()
    key = upload_bundle(store, p, 0, pair_responses("monex", "7203", received_at_utc=RECEIVED))
    original = store.objects[key][0]
    changed = {
        "empty-gzip-member": lambda: original + gzip.compress(b""),
        "small-gzip-member": lambda: original + gzip.compress(b"x"),
        "excess-padding": lambda: gzip.compress(gzip.decompress(original) + bytes(40000)),
        # Lie in the unused member's ISIZE so the reader must check the
        # bounded decompressed tail, not only its final four compressed bytes.
        "large-member-with-false-size": lambda: (
            original + gzip.compress(bytes(2 * 1024**2))[:-4] + original[-4:]
        ),
    }[extra]()
    store.seed(key, changed, "application/gzip")

    class BoundedGzip(gzip.GzipFile):
        def seek(self, offset, whence=io.SEEK_SET):
            assert whence != io.SEEK_END
            return super().seek(offset, whence)

        def read(self, size=-1):
            # Member bodies in this fixture are small; checking unreferenced
            # data may read at most one record, three blocks, and never all.
            assert 0 <= size <= tarfile.RECORDSIZE + 3 * tarfile.BLOCKSIZE
            return super().read(size)

    monkeypatch.setattr(external_assessment_raw.gzip, "GzipFile", BoundedGzip)
    with pytest.raises(ValueError, match="invalid external assessment raw"):
        external_assessment_raw.read_raw_bundle(store, key)


def _rewrite_manifest(store, key, change):
    with tarfile.open(fileobj=io.BytesIO(store.objects[key][0]), mode="r:gz") as original:
        members = [(item.name, original.extractfile(item).read()) for item in original.getmembers()]
    manifest = json.loads(members[0][1])
    change(manifest)
    members[0] = ("manifest.json", json.dumps(manifest).encode())
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, body in members:
            member = tarfile.TarInfo(name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    store.seed(key, output.getvalue(), "application/gzip")


@pytest.mark.parametrize(
    "change",
    [
        lambda manifest: manifest.update(format="external-assessment-cohort-raw-v1"),
        lambda manifest: manifest["receipts"][1].update(parent_receipt_id=str(uuid.uuid4())),
        lambda manifest: manifest["receipts"][1].update(attempt_id=str(uuid.uuid4())),
        lambda manifest: manifest["receipts"][0].update(extra="unexpected"),
        lambda manifest: manifest["receipts"][0].update(requested_ticker="７２０３"),
    ],
)
def test_readback_rejects_old_or_mismatched_raw_identity(change):
    p, store = plan(sources=("rakuten",)), FakeObjectStore()
    originals = pair_responses("rakuten", "7203", received_at_utc=RECEIVED)
    key = upload_bundle(store, p, 0, originals)
    _rewrite_manifest(store, key, change)
    with pytest.raises(ValueError, match=r"external assessment|canonical"):
        external_assessment_raw.read_raw_bundle(store, key)


def test_outer_page_only_is_valid_original_but_incomplete_for_l2():
    store = FakeObjectStore()
    outer = response("rakuten", "7203", official_html().encode())
    with external_assessment_raw.RawBundle("outer-only", 0) as bundle:
        bundle.append(outer)
        key = bundle.finish()
        bundle.upload(store)
    _, restored = external_assessment_raw.read_raw_bundle(store, key)
    assert restored == (outer,)
    with pytest.raises(ParseError, match="incomplete_attempt"):
        decode_attempt(restored)


def legacy_bundle(store, p, *, body=b"legacy original"):
    with external_assessment_raw.RawBundle(p.run_id, 0) as bundle:
        bundle.append(response("monex", "7203", body))
        bundle.finish()
        bundle.upload(store)
        retained = bundle.key
    legacy = retained.replace(
        external_assessment_raw.RETAINED_RAW_PREFIX, external_assessment_raw.RAW_PREFIX, 1
    )
    payload = store.objects[retained][0]
    store.seed(legacy, payload, "application/gzip")
    del store.objects[retained]
    store.puts.clear()
    return legacy, retained, payload


@pytest.mark.parametrize("retained_exists", [True, False])
def test_bundle_reader_gets_the_exact_selected_bytes_without_a_redundant_head(
    monkeypatch, retained_exists
):
    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            return RECEIVED.astimezone(tz)

    ids = iter((uuid.UUID(int=1), uuid.UUID(int=2)))
    monkeypatch.setattr(external_assessment_raw, "datetime", Clock)
    monkeypatch.setattr(external_assessment_raw, "uuid", SimpleNamespace(uuid4=lambda: next(ids)))
    store, p = FakeObjectStore(), plan()
    legacy, retained, _ = legacy_bundle(store, p, body=b"old bytes")
    suffix = f"utc-date/2026-10-11/bundles/{p.run_id}/cohort-0000-{1:032x}.tar.gz"
    assert retained == external_assessment_raw.RETAINED_RAW_PREFIX + suffix
    assert legacy == external_assessment_raw.RAW_PREFIX + suffix
    if retained_exists:
        _, _, other = legacy_bundle(store, p, body=b"distinct retained bytes")
        store.seed(retained, other, "application/gzip")

    def response(key):
        store.events.append(("GET-with-metadata", key))
        return store.objects.get(key)

    monkeypatch.setattr(store, "get_bytes_with_metadata", response, raising=False)
    store.events.clear()
    _, (raw,) = external_assessment_raw.read_raw_bundle(store, legacy)
    assert raw.body == (b"distinct retained bytes" if retained_exists else b"old bytes")
    assert store.events == (
        [("HEAD", retained), ("GET", retained)]
        if retained_exists
        else [("HEAD", retained), ("HEAD", legacy), ("GET", legacy)]
    )
    store.events.clear()
    if retained_exists:
        _, (direct,) = external_assessment_raw.read_raw_bundle(store, retained)
        assert direct == raw
    else:
        with pytest.raises(ValueError, match="missing"):
            external_assessment_raw.read_raw_bundle(store, retained)
    assert store.events == [("GET-with-metadata", retained)] + (
        [] if retained_exists else [("HEAD", retained)]
    )
    if not retained_exists:
        del store.objects[legacy]
        store.events.clear()
        with pytest.raises(ValueError, match="missing"):
            external_assessment_raw.read_raw_bundle(store, legacy)
        assert store.events == [("HEAD", retained), ("HEAD", legacy)]


@pytest.mark.parametrize(
    ("get_reply", "head_reply", "error"),
    [
        ("404", "404", ValueError),
        ("NoSuchKey", "200", R2Error),
        ("NotFound", "403", R2Error),
        ("404", "429", R2Error),
        ("403", None, R2Error),
        ("429", None, R2Error),
        ("500", None, R2Error),
        ("invalid", None, ValueError),
    ],
)
def test_exact_current_key_errors_remain_source_local_only_for_confirmed_absence_or_invalid_bytes(
    monkeypatch, get_reply, head_reply, error
):
    store = Boto3R2Store(
        bucket="fixture",
        env={"R2_ACCOUNT_ID": "a" * 32, "R2_ACCESS_KEY_ID": "fake", "R2_SECRET_ACCESS_KEY": "fake"},
    )
    key = external_assessment_raw.RETAINED_RAW_PREFIX + (
        "utc-date/2026-10-11/bundles/synthetic-20261011/cohort-0000-" + "0" * 32 + ".tar.gz"
    )
    calls = []

    def reply(method, code, params):
        calls.append((method, params))
        if code in {"200", "invalid"}:
            return {"ETag": '"stamp"', "ContentLength": 4, "Body": io.BytesIO(b"oops")}
        raise ClientError({"Error": {"Code": code}}, method)

    client = SimpleNamespace(
        get_object=lambda **params: reply("GET", get_reply, params),
        head_object=lambda **params: reply("HEAD", head_reply, params),
    )
    monkeypatch.setattr(store, "_client", lambda: client)
    with pytest.raises(error):
        external_assessment_raw.read_raw_bundle(store, key)
    assert calls == [
        (method, {"Bucket": "fixture", "Key": key})
        for method in (["GET", "HEAD"] if head_reply else ["GET"])
    ]


@pytest.mark.parametrize("fault", ["404", "403", "429", "invalid"])
@pytest.mark.parametrize("retained_exists", [True, False])
def test_a_failed_get_after_positive_head_never_reselects(monkeypatch, fault, retained_exists):
    store = FakeObjectStore()
    legacy, retained, payload = legacy_bundle(store, plan())
    if retained_exists:
        store.seed(retained, payload)

    def body(key):
        assert key == (retained if retained_exists else legacy)
        if fault != "invalid":
            raise R2Error(fault)
        return b"not a tar archive"

    monkeypatch.setattr(store, "get_bytes", body)
    monkeypatch.setattr(
        store, "get_bytes_with_metadata", lambda _: pytest.fail("reselected key"), raising=False
    )
    with pytest.raises(ValueError if fault == "invalid" else R2Error, match=fault):
        external_assessment_raw.read_raw_bundle(store, legacy)


@pytest.mark.parametrize("legacy_reference", [False, True])
def test_verification_uses_the_exact_retained_native_record_without_copying(legacy_reference):
    store, p = FakeObjectStore(), plan()
    legacy, retained, payload = legacy_bundle(store, p)
    expected = external_assessment_raw.read_raw_bundle(store, legacy)
    store.seed(retained, payload, "application/gzip")
    del store.objects[legacy]
    before = dict(store.objects)
    key = legacy if legacy_reference else retained
    external_assessment_raw.verify_retained_raw_bundle(store, key)
    assert external_assessment_raw.read_raw_bundle(store, key) == expected
    assert store.objects == before
    assert store.puts == []


def test_conflicting_retained_copy_is_not_accepted_or_overwritten():
    store, p = FakeObjectStore(), plan()
    legacy, retained, _ = legacy_bundle(store, p)
    _, _, other = legacy_bundle(store, p, body=b"different original")
    store.seed(retained, other, "application/gzip")
    before = dict(store.objects)
    with pytest.raises(ValueError, match="conflicts with legacy"):
        external_assessment_raw.verify_retained_raw_bundle(store, legacy)
    assert store.objects == before
    assert store.puts == []


@pytest.mark.parametrize("exists_in_old_prefix", [False, True])
def test_cumulative_ready_prerequisite_never_copies_or_recreates_legacy_raw(exists_in_old_prefix):
    store, p = FakeObjectStore(), plan()
    legacy, retained, _ = legacy_bundle(store, p)
    if exists_in_old_prefix:
        # Exact historical resolution remains read-only; it is not durable readiness.
        assert external_assessment_raw.read_raw_bundle(store, legacy)[1][0].body == (
            b"legacy original"
        )
    else:
        del store.objects[legacy]
    before = dict(store.objects)
    with pytest.raises(ValueError, match="retained external original is missing"):
        external_assessment_raw.verify_retained_raw_bundle(store, legacy)
    assert retained not in store.objects
    assert store.objects == before
    assert store.puts == []


@pytest.mark.parametrize(
    ("legacy", "get_status", "head_status", "result"),
    [
        (False, 404, 404, None),
        (False, 403, 404, R2Error),  # An absence-looking service code does not override status.
        (False, 429, 404, R2Error),
        (False, 503, 404, R2Error),
        (False, None, 404, R2Error),
        (False, 404, 401, R2Error),
        (False, 404, None, R2Error),
        (False, 404, 200, R2Error),  # GET/HEAD disagreement is not absence.
        (False, 200, 404, ValueError),  # A present, corrupt body never authorizes ACK reuse.
        (True, 404, 404, None),  # Both locations for this exact reference must be absent.
        (True, 404, 429, R2Error),
        (True, 404, 200, R2Error),  # A selected present alias never falls back after GET failure.
    ],
)
def test_missing_bundle_can_be_recovered_only_after_explicit_absence(
    monkeypatch, legacy, get_status, head_status, result
):
    store = Boto3R2Store(
        bucket="fixture",
        env={"R2_ACCOUNT_ID": "a" * 32, "R2_ACCESS_KEY_ID": "fake", "R2_SECRET_ACCESS_KEY": "fake"},
    )
    suffix = "utc-date/2026-10-11/bundles/synthetic-20261011/cohort-0000-" + "0" * 32 + ".tar.gz"
    retained = external_assessment_raw.RETAINED_RAW_PREFIX + suffix
    key = external_assessment_raw.RAW_PREFIX + suffix if legacy else retained
    calls = []

    def respond(method, status, params):
        calls.append((method, params["Key"]))
        if status == 200:
            return {"ETag": '"stamp"', "ContentLength": 4, "Body": io.BytesIO(b"oops")}
        raise ClientError(
            {"Error": {"Code": "NotFound"}, "ResponseMetadata": {"HTTPStatusCode": status}},
            method,
        )

    client = SimpleNamespace(
        get_object=lambda **params: respond("GET", get_status, params),
        head_object=lambda **params: respond("HEAD", head_status, params),
    )
    monkeypatch.setattr(store, "_client", lambda: client)
    if result is None:
        assert external_assessment_raw.read_raw_bundle(store, key, allow_missing=True) is None
    else:
        with pytest.raises(result):
            external_assessment_raw.read_raw_bundle(store, key, allow_missing=True)
    if legacy:
        expected = [("HEAD", retained)]
        if head_status == 200:
            expected.append(("GET", retained))
        elif head_status == 404:
            expected.append(("HEAD", key))
    else:
        expected = [("GET", key)]
        if get_status == 404:
            expected.append(("HEAD", key))
    assert calls == expected
