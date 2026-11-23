"""The cutoff witness reads the ordinary BOJ owner's frozen plan, never the provider."""

import json

import pytest
from tests.helpers.article_acquisition import SLOT, Response, Session
from tests.helpers.r2_store import MemoryR2Store

from baibai_jobs.acquisition import articles
from baibai_jobs.acquisition.article_planning import publish_planning_input

SOURCE = "boj_news"
PRIVATE = "assert-no-bodies-or-urls-in-witness"


def _rss(count, *, foreign, rdf):
    items = "".join(
        f"<item><title>{PRIVATE}</title><link>https://www.boj.or.jp/{PRIVATE}-{i}.htm</link></item>"
        for i in range(count)
    )
    if foreign:
        items += (
            f"<extension:item><extension:title>{PRIVATE}</extension:title>"
            f"<extension:link>https://www.boj.or.jp/{PRIVATE}-extension.htm</extension:link>"
            "</extension:item>"
        )
    if rdf:
        return f'<rdf:RDF xmlns:rdf="urn:test:rdf">{items}</rdf:RDF>'.encode()
    return f'<rss xmlns:extension="urn:test:foreign"><channel>{items}</channel></rss>'.encode()


def _freeze(tmp_path, monkeypatch, *, foreign=False, nested=False, rdf=False, count=17):
    store = MemoryR2Store()
    publish_planning_input(store, SOURCE, {})
    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    body = (
        f'<main>{PRIVATE}<a href="/{PRIVATE}-child.pdf">PDF</a></main>'.encode()
        if nested
        else b"<main>Plain document</main>"
    )
    session = Session(
        [
            Response(200, _rss(count - (foreign or nested), foreign=foreign, rdf=rdf)),
            Response(200, body),
            *[Response(200, b"<main>Plain document</main>") for _ in range(7)],
        ]
    )
    # Produce real pre-cutoff native bytes and unversioned RSS coordinates with the ordinary writer.
    with monkeypatch.context() as legacy:
        legacy.setattr(articles, "RSS_SOURCES", articles.RSS_SOURCES - {SOURCE})
        report = articles.acquire_source(
            store,
            source=SOURCE,
            slot=SLOT,
            identity="frozen",
            directory=tmp_path / "frozen",
            session=session,
        )
    before = dict(store.values)

    def no_mutation(*_args, **_kwargs):
        pytest.fail("verification tried to use publication/acquisition")

    monkeypatch.setattr(store, "close", lambda: None, raising=False)
    monkeypatch.setattr(store, "put_file", no_mutation)
    monkeypatch.setattr(articles, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(articles.requests, "Session", no_mutation)
    return store, report, before


def _invoke(stage, *, stage_id=None, digest=None):
    return articles.main(
        [
            "verify-boj-pending",
            "--stage-id",
            stage_id or stage["stage_id"],
            "--manifest-sha256",
            digest or stage["manifest_sha256"],
        ]
    )


@pytest.mark.parametrize(
    ("foreign", "nested", "rdf", "expected", "exit_code"),
    [
        (False, False, False, (9, 0, 0), 0),
        (True, False, False, (8, 1, 0), 1),
        (False, True, False, (8, 0, 1), 1),
        (False, False, True, (0, 0, 9), 1),
    ],
)
def test_exact_frozen_boj_proof_distinguishes_native_legacy_and_unverified_children(
    tmp_path, monkeypatch, capsys, foreign, nested, rdf, expected, exit_code
):
    store, stage, before = _freeze(tmp_path, monkeypatch, foreign=foreign, nested=nested, rdf=rdf)
    assert stage["pending_url_count"] == 9
    assert _invoke(stage) == exit_code
    lines = capsys.readouterr()
    assert PRIVATE not in lines.out
    assert lines.err == ""
    native, incompatible, unverified = expected
    assert json.loads(lines.out) == {
        "source": SOURCE,
        "stage_id": stage["stage_id"],
        "manifest_sha256": stage["manifest_sha256"],
        "pending_url_count": 9,
        "native_seed_count": native,
        "incompatible_count": incompatible,
        "unverified_count": unverified,
    }
    assert store.values == before


@pytest.mark.parametrize("change", ["stage", "manifest", "count", "moving_head"])
def test_unfixed_or_changed_boj_plan_can_never_issue_a_witness(
    tmp_path, monkeypatch, capsys, change
):
    _, stage, _ = _freeze(tmp_path, monkeypatch, count=18 if change == "count" else 17)
    if change == "moving_head":
        load = articles.load_planning
        calls = []

        def advancing(*args):
            state, etag = load(*args)
            calls.append(etag)
            return state, etag + "-different" if len(calls) > 1 else etag

        monkeypatch.setattr(articles, "load_planning", advancing)
    assert (
        _invoke(
            stage,
            stage_id="wrong-stage" if change == "stage" else None,
            digest="0" * 64 if change == "manifest" else None,
        )
        == 1
    )
    lines = capsys.readouterr()
    assert lines.out == ""
    assert lines.err.startswith("BOJ pending verification failed: ")
    assert PRIVATE not in lines.err


def test_missing_bound_receipt_body_does_not_issue_a_witness(tmp_path, monkeypatch, capsys):
    store, stage, _ = _freeze(tmp_path, monkeypatch)
    (key,) = [key for key in store.values if stage["stage_id"] in key and "/receipts-" in key]
    del store.values[key]
    assert _invoke(stage) == 1
    lines = capsys.readouterr()
    assert lines.out == ""
    assert lines.err.startswith("BOJ pending verification failed: ")
    assert PRIVATE not in lines.err
