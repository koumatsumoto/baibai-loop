"""The cutoff witness reads the ordinary BOJ owner's frozen plan, never the provider."""

import json
from datetime import timedelta

import pytest
from tests.helpers.article_acquisition import SLOT, Response, Session
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.receipts.articles.contracts import digest_json
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


def _freeze(
    tmp_path,
    monkeypatch,
    *,
    foreign=False,
    nested=0,
    rdf=False,
    count=17,
    edit_cursor=None,
    archived_parent=False,
):
    store = MemoryR2Store()
    publish_planning_input(store, SOURCE, {})
    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    attachments = "".join(f'<a href="/{PRIVATE}-{i}.pdf">PDF</a>' for i in range(nested))
    body = f"<main>{PRIVATE}{attachments}</main>".encode()
    if archived_parent:
        old = articles.acquire_source(
            store,
            source=SOURCE,
            slot=SLOT,
            identity="initial-200",
            directory=tmp_path / "initial",
            session=Session(
                [Response(200, _rss(1, foreign=False, rdf=False)), Response(200, body)]
                + [Response(200, b"<main>Initial child</main>") for _ in range(nested)]
            ),
        )
        assert old["pending_url_count"] == 0
    session = Session(
        [
            Response(200, _rss(count - int(foreign) - nested, foreign=foreign, rdf=rdf)),
            Response(304, b"") if archived_parent else Response(200, body),
            *[Response(200, b"<main>Plain document</main>") for _ in range(7)],
        ]
    )
    # Produce real pre-cutoff native bytes and unversioned RSS coordinates with the ordinary writer.
    with monkeypatch.context() as legacy:
        legacy.setattr(articles, "RSS_SOURCES", articles.RSS_SOURCES - {SOURCE})
        if edit_cursor is not None:
            commit = articles.commit_stage

            def freeze_edited_cursor(*args, **kwargs):
                edit_cursor(kwargs["cursor"])
                return commit(*args, **kwargs)

            legacy.setattr(articles, "commit_stage", freeze_edited_cursor)
        report = articles.acquire_source(
            store,
            source=SOURCE,
            slot=SLOT + timedelta(hours=4 * archived_parent),
            identity="frozen",
            directory=tmp_path / "frozen",
            session=session,
        )
    if archived_parent:
        report = articles.acquire_source(
            store,
            source=SOURCE,
            slot=SLOT + timedelta(hours=8),
            identity="active-anchor",
            directory=tmp_path / "active",
            session=Session([Response(403, b"")]),
        )
        assert report["pending_url_count"] == 9
    before = dict(store.values)

    def no_mutation(*_args, **_kwargs):
        pytest.fail("verification tried to use publication/acquisition")

    monkeypatch.setattr(store, "close", lambda: None, raising=False)
    monkeypatch.setattr(store, "put_file", no_mutation)
    monkeypatch.setattr(store, "iter_objects", no_mutation)
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
    ("foreign", "nested", "rdf", "expected", "reasons", "exit_code"),
    [
        (False, 0, False, (9, 0, 0, 0), {}, 0),
        (True, 0, False, (8, 0, 1, 0), {}, 1),
        (False, 2, False, (7, 2, 0, 0), {}, 0),
        (False, 0, True, (0, 0, 0, 9), {"index_not_rss": 9}, 1),
    ],
)
def test_exact_frozen_boj_proof_distinguishes_native_legacy_and_unverified_children(
    tmp_path, monkeypatch, capsys, foreign, nested, rdf, expected, reasons, exit_code
):
    store, stage, before = _freeze(tmp_path, monkeypatch, foreign=foreign, nested=nested, rdf=rdf)
    assert stage["pending_url_count"] == 9
    store.get_keys.clear()
    assert _invoke(stage) == exit_code
    lines = capsys.readouterr()
    assert PRIVATE not in lines.out
    assert lines.err == ""
    native_seed, native_child, incompatible, unverified = expected
    assert json.loads(lines.out) == {
        "source": SOURCE,
        "stage_id": stage["stage_id"],
        "manifest_sha256": stage["manifest_sha256"],
        "pending_url_count": 9,
        "native_seed_count": native_seed,
        "native_child_count": native_child,
        "incompatible_count": incompatible,
        "unverified_count": unverified,
        "unverified_reason_counts": reasons,
    }
    assert store.values == before
    assert len([key for key in store.get_keys if "/receipts-" in key]) == 1
    assert {key.rsplit("/", 1)[-1] for key in store.get_keys if "/receipts-" not in key} == {
        "boj_news.json",
        "handoff.json",
    }


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


@pytest.mark.parametrize(
    ("defect", "expected"),
    [
        ("locator", {"child_locator_unverified": 1}),
        ("extra_edge", {"child_locator_unverified": 1}),
        ("revision", {"discovery_revision_unverified": 1}),
        ("parent_sha", {"parent_receipt_unverified": 1}),
        ("parent_encoding", {"parent_receipt_unverified": 1}),
        ("parent_body_ref", {"parent_receipt_unverified": 1}),
        ("absent_parent_index", {"parent_index_unverified": 1}),
        ("missing_seed", {"no_retained_evidence": 1}),
    ],
)
def test_boj_evidence_disagreement_is_counted_without_treating_a_child_as_a_seed(
    tmp_path, monkeypatch, capsys, defect, expected
):
    def edit(cursor):
        if defect == "missing_seed":
            cursor["pending_evidence"].pop(next(iter(cursor["pending_evidence"])))
            return
        edges = next(iter(cursor["discovery_children"].values()))
        if defect == "locator":
            edges[0]["locator"] += "-wrong"
        elif defect == "extra_edge":
            edges.append({**edges[0], "locator": edges[0]["locator"] + "-wrong"})
        elif defect == "revision":
            edges[0]["discovery_revision"] += "-old"
        else:
            parents = cursor["discovery_parents"]
            key = edges[0]["parent"]
            parent = dict(parents[key])
            if defect == "parent_sha":
                parent["receipt_sha256"] = "0" * 64
            elif defect == "parent_encoding":
                parent["encoding"] = "ascii"
            elif defect == "parent_body_ref":
                parent["body_ref"] = {**parent["body_ref"], "sha256": "0" * 64}
            else:
                assert defect == "absent_parent_index"
                parent["index_evidence"] = None
            new_key = digest_json(parent)
            parents[new_key] = parent
            edges[0]["parent"] = new_key

    store, stage, before = _freeze(tmp_path, monkeypatch, nested=2, edit_cursor=edit)
    assert _invoke(stage) == 1
    lines = capsys.readouterr()
    assert lines.err == ""
    assert PRIVATE not in lines.out
    result = json.loads(lines.out)
    assert result["native_seed_count"] == (6 if defect == "missing_seed" else 7)
    assert result["native_child_count"] == (2 if defect == "missing_seed" else 1)
    assert result["incompatible_count"] == 0
    assert result["unverified_count"] == 1
    assert result["unverified_reason_counts"] == expected
    assert store.values == before


def test_fixed_prior_packet_can_prove_children_bound_to_a_parent_304(tmp_path, monkeypatch, capsys):
    store, stage, before = _freeze(tmp_path, monkeypatch, nested=2, archived_parent=True)
    store.get_keys.clear()
    assert _invoke(stage) == 0
    lines = capsys.readouterr()
    assert lines.err == ""
    assert PRIVATE not in lines.out
    result = json.loads(lines.out)
    assert result["native_seed_count"] == 7
    assert result["native_child_count"] == 2
    (packet,) = [key for key in store.get_keys if "/receipts-" in key]
    assert "-frozen/" in packet
    assert store.values == before


def test_over_budget_child_edges_fail_before_downloading_any_packet(tmp_path, monkeypatch, capsys):
    def edit(cursor):
        edges = next(iter(cursor["discovery_children"].values()))
        edges *= 19

    store, stage, before = _freeze(tmp_path, monkeypatch, nested=2, edit_cursor=edit)
    store.get_keys.clear()
    assert _invoke(stage) == 1
    lines = capsys.readouterr()
    assert lines.out == ""
    assert lines.err == "BOJ pending verification failed: ValueError\n"
    assert not any("/receipts-" in key for key in store.get_keys)
    assert store.values == before


def test_parent_omitted_from_manifest_receipt_ids_does_not_prove_children(
    tmp_path, monkeypatch, capsys
):
    commit = articles.commit_stage

    def commit_without_parent_receipt(*args, **kwargs):
        parent = next(iter(kwargs["cursor"]["discovery_parents"].values()))
        path = kwargs["directory"] / "result.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        receipt_id = parent["receipt_id"]
        assert report["projection_kinds"][receipt_id] == "document"
        # The row, kind and content hashes are intact; the canonical list omits it.
        report["receipt_ids"].remove(receipt_id)
        path.write_text(json.dumps(report), encoding="utf-8")
        return commit(*args, **kwargs)

    monkeypatch.setattr(articles, "commit_stage", commit_without_parent_receipt)
    store, stage, before = _freeze(tmp_path, monkeypatch, nested=2)
    assert _invoke(stage) == 1
    lines = capsys.readouterr()
    assert lines.err == ""
    assert PRIVATE not in lines.out
    result = json.loads(lines.out)
    assert result["native_seed_count"] == 7
    assert result["native_child_count"] == 0
    assert result["unverified_count"] == 2
    assert result["unverified_reason_counts"] == {"parent_receipt_unverified": 2}
    assert store.values == before
