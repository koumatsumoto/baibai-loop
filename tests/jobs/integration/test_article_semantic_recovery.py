"""Explicit bounded refresh adds meaning without rewriting originals or past cutoffs."""

import hashlib
import json
import subprocess
import sys
from contextlib import closing
from copy import deepcopy
from datetime import timedelta

import pytest
from tests.helpers.trends import BOJ, NOW

from baibai_engine.data_collection.receipts.articles.contracts import ArticleReceipt, BodyRef
from baibai_engine.data_collection.receipts.articles.store import retain_receipt
from baibai_engine.machine_analysis.generation.runners.trend_inputs import read_trend_inputs
from baibai_engine.machine_analysis.observations.articles.projection import project_receipt
from baibai_engine.machine_analysis.observations.articles.store import read_artifacts, save_artifact
from baibai_engine.machine_analysis.observations.trends.extraction import extract_observation
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration.article_semantic_recovery import (
    apply_recovery,
    decode_recovery,
    result_sha256,
)
from baibai_jobs.integration.workflow_inputs import WorkflowInputError, validate_daily_input


@pytest.fixture
def compared(tmp_path):
    database = tmp_path / "market.sqlite"
    targets, old = [], []
    with closing(open_connection(database)) as conn, conn:
        for i, text in enumerate((BOJ, "合成の資金供給オペ実施結果。政策決定の記事ではない。")):
            body = f"<main><h1>当面の金融政策運営について</h1><p>{text}</p></main>".encode()
            receipt = ArticleReceipt(
                receipt_id=f"original-{i}",
                source="boj_news",
                requested_url=f"https://www.boj.or.jp/mopo/mpmdeci/mpr_2026/synthetic-{i}.htm",
                received_at=NOW - timedelta(days=2),
                status="ok",
                http_status=200,
                body_ref=BodyRef(
                    receipt_id=f"original-{i}", sha256=hashlib.sha256(body).hexdigest()
                ),
            )
            retain_receipt(conn, receipt, body=body)
            _, (observation,) = project_receipt(
                conn, receipt.receipt_id, kind="document", generated_at=receipt.received_at
            )
            fresh = extract_observation(observation, receipt, generated_at=NOW)
            prior = fresh.model_copy(
                update={
                    "extractor_revision": "boj-policy-decision-v1",
                    "generated_at": receipt.received_at,
                }
            )
            save_artifact(conn, prior)
            old.append(prior)
            targets.append(
                {
                    "observation_id": observation.observation_id,
                    "observation_sha256": observation.sha256,
                    "article_version_id": observation.article_version_id,
                    "extractor_revision": fresh.extractor_revision,
                    "result_sha256": result_sha256(fresh),
                }
            )
    return database, {"as_of": NOW.date().isoformat(), "observations": targets}, old


def apply(compared, *, at=NOW):
    database, request, _ = compared
    return apply_recovery(
        raw=json.dumps(request), database=database, asof=NOW.date(), generated_at=at
    )


def rows(database):
    with closing(open_connection(database)) as conn:
        return {
            name: [tuple(row) for row in conn.execute(f"SELECT * FROM {name} ORDER BY 1")]
            for name in (
                "article_source_receipts",
                "article_projection_runs",
                "article_observations",
                "article_extraction_artifacts",
            )
        }


def fixed(database, cutoff):
    return read_trend_inputs(
        database, release_id="synthetic", release_sha256="a" * 64, cutoff=cutoff, as_of=NOW.date()
    )


def test_explicit_versions_append_at_now_preserve_originals_and_old_cutoff(compared):
    database, request, old = compared
    before = rows(database)
    past = fixed(database, NOW - timedelta(seconds=1))
    result = apply(compared)
    assert result["status"] == "applied"
    assert result["observation_count"] == 2
    assert set(result["artifact_statuses"].values()) == {"extracted", "unsupported"}
    after = rows(database)
    for name in before:
        if name != "article_extraction_artifacts":
            assert before[name] == after[name]
    assert len(after["article_extraction_artifacts"]) == 4
    with closing(open_connection(database)) as conn:
        assert read_artifacts(conn, [a.artifact_id for a in old]) == tuple(old)
        new = read_artifacts(conn, result["artifact_ids"])
    assert all(a.generated_at == NOW for a in new)
    assert fixed(database, NOW - timedelta(seconds=1)) == past
    current = fixed(database, NOW)
    assert current.social.coverage.state == "partial"
    assert sum(len(a.current.claims) for a in current.social.artifacts) == 2
    assert {a.current.evidence.observation_id for a in current.social.artifacts} == {
        t["observation_id"] for t in request["observations"]
    }
    assert set(result["article_delta"]["articles.extraction_artifacts"]) == {
        a.artifact_id for a in (*old, *new)
    }
    repeated = apply(compared, at=NOW + timedelta(minutes=1))
    assert repeated["artifact_ids"] == result["artifact_ids"]
    assert repeated["article_delta"] == result["article_delta"]
    assert rows(database) == after


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("article_version_id", "article-version-" + "0" * 64),
        ("extractor_revision", "boj-policy-decision-v999"),
        ("result_sha256", "0" * 64),
    ],
)
def test_mismatched_last_target_rolls_back_all_artifacts(compared, field, value):
    database, request, _ = compared
    request["observations"][-1][field] = value
    before = rows(database)
    with pytest.raises(ValueError, match="article recovery"):
        apply(compared)
    assert rows(database) == before


def test_corrupt_body_is_rejected_without_native_reprojection(compared):
    database, _, _ = compared
    with closing(open_connection(database)) as conn, conn:
        conn.execute(
            "UPDATE article_source_receipts SET body_gzip=X'00' WHERE receipt_id='original-1'"
        )
    before = rows(database)
    with pytest.raises((OSError, ValueError)):
        apply(compared)
    assert rows(database) == before


@pytest.mark.parametrize("offset", [-3, None])
def test_generation_clock_cannot_precede_receipt_or_be_naive(compared, offset):
    before = rows(compared[0])
    at = NOW.replace(tzinfo=None) if offset is None else NOW + timedelta(days=offset)
    with pytest.raises(ValueError, match="article recovery"):
        apply(compared, at=at)
    assert rows(compared[0]) == before


def test_missing_database_is_not_initialized(compared, tmp_path):
    absent = tmp_path / "absent.sqlite"
    with pytest.raises(ValueError, match="existing market"):
        apply_recovery(
            raw=json.dumps(compared[1]), database=absent, asof=NOW.date(), generated_at=NOW
        )
    assert not absent.exists()


@pytest.mark.parametrize(
    "change",
    [
        "empty",
        "duplicate",
        "too_many",
        "unknown",
        "unknown_target",
        "bad_id",
        "bad_sha",
        "bad_version",
        "bad_revision",
        "bad_result",
        "same_version",
        "bad_date",
    ],
)
def test_input_rejects_implicit_or_ambiguous_scope_before_credentials(compared, change):
    request = deepcopy(compared[1])
    target = request["observations"][0]
    if change == "empty":
        request["observations"] = []
    elif change == "duplicate":
        request["observations"].append(target)
    elif change == "too_many":
        request["observations"] = [target] * 17
    elif change == "unknown":
        request["all_sources"] = True
    elif change == "unknown_target":
        target["url"] = "https://example.invalid"
    elif change == "bad_id":
        target["observation_id"] = "$(exit 7)"
    elif change == "bad_sha":
        target["observation_sha256"] = "0" * 64
    elif change == "bad_version":
        target["article_version_id"] = None
    elif change == "bad_revision":
        target["extractor_revision"] = "other-v1"
    elif change == "bad_result":
        target["result_sha256"] = 0
    elif change == "same_version":
        request["observations"][1]["article_version_id"] = target["article_version_id"]
    else:
        request["as_of"] = "2026-1-1"
    with pytest.raises((ValueError, TypeError)):
        decode_recovery(json.dumps(request))


def test_explicit_retention_and_date_are_required_only_when_opted_in(compared):
    raw = json.dumps(compared[1])
    validate_daily_input(asof="", preserve_existing_generations="")
    validate_daily_input(
        asof=NOW.date().isoformat(),
        preserve_existing_generations="true",
        article_semantic_recovery=raw,
    )
    for asof, preserve in (("", "true"), ("2026-01-01", "true"), (NOW.date().isoformat(), "false")):
        with pytest.raises(WorkflowInputError):
            validate_daily_input(
                asof=asof, preserve_existing_generations=preserve, article_semantic_recovery=raw
            )
    with pytest.raises(WorkflowInputError):
        validate_daily_input(
            asof=NOW.date().isoformat(),
            preserve_existing_generations="true",
            article_semantic_recovery="x" * 16_385,
        )


def test_precredential_validator_does_not_import_engine(compared):
    raw = json.dumps(compared[1])
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from baibai_jobs.integration.article_semantic_recovery import decode_recovery; import sys; decode_recovery(sys.argv[1]); assert not any(k.startswith('baibai_engine') for k in sys.modules)",
            raw,
        ],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_cli_apply_and_verify_require_lease_and_preserve_private_report(
    compared, tmp_path, monkeypatch, capsys
):
    from baibai_engine.storage.objects import r2
    from baibai_jobs.integration import sources
    from baibai_jobs.integration.article_semantic_recovery import main
    from baibai_jobs.publication import article_verification, lease

    class Store:
        def close(self):
            pass

    store = Store()
    calls = []
    monkeypatch.setattr(r2, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(lease, "_load_handle", lambda _: "fixed-handle")
    monkeypatch.setattr(lease, "verify_held", lambda s, h: calls.append((s, h)))
    monkeypatch.setattr(
        sources,
        "canonical_generations",
        lambda s: {"lake_release_id": "new", "lake_manifest_sha256": "b" * 64},
    )

    def verify(s, *, delta, directory, generations):
        assert s is store
        assert generations["lake_release_id"] == "new"
        assert len(delta["articles.extraction_artifacts"]) == 4
        return {k: len(v) for k, v in delta.items()}

    monkeypatch.setattr(article_verification, "verify_delta", verify)
    conditions, report = tmp_path / "conditions.json", tmp_path / "report.json"
    conditions.write_text(json.dumps(compared[1]))
    args = [
        "--conditions",
        str(conditions),
        "--report",
        str(report),
        "--market",
        str(compared[0]),
        "--asof",
        NOW.date().isoformat(),
        "--lease-handle",
        str(tmp_path / "handle"),
    ]
    assert main(["apply", *args]) == 0
    assert main(["verify", *args]) == 0
    assert len(calls) == 4
    final = json.loads(report.read_text())
    assert final["status"] == "published_and_verified"
    assert final["canonical_generations"]["lake_release_id"] == "new"
    public = capsys.readouterr().out
    assert "original-0" not in public
    assert "article-observation-" not in public
    # A changed contract cannot authorize readback under another request.
    conditions.write_text(conditions.read_text() + " ")
    assert main(["verify", *args]) == 1
    assert json.loads(report.read_text()) == final
