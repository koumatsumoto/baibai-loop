"""Native TSR/BOJ projection and Trend selection share an exact receipt clock."""

from contextlib import closing
from datetime import timedelta, timezone
from hashlib import sha256

import pytest
from tests.helpers.trends import BOJ, NOW, PHARMA

from baibai_engine.data_collection.receipts.articles.contracts import ArticleReceipt, BodyRef
from baibai_engine.data_collection.receipts.articles.store import retain_receipt
from baibai_engine.machine_analysis.generation.runners.trend_inputs import read_trend_inputs
from baibai_engine.machine_analysis.observations.articles.projection import project_receipt
from baibai_engine.machine_analysis.observations.articles.store import save_artifact
from baibai_engine.machine_analysis.observations.trends.extraction import (
    extract_observation,
    extract_observations,
    matching_artifacts,
)
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

CUTOFF = NOW.replace(hour=12, microsecond=100_000)
US = timedelta(microseconds=1)
EAST = timezone(timedelta(hours=9))
WEST = timezone(timedelta(hours=-4))


@pytest.fixture(params=["tsr_data", "boj_news"])
def source(request):
    return request.param


def retain_document(
    conn, source, identity, *, received_at, version=0, url_key="fixed", failed_at=None
):
    if source == "tsr_data":
        url = f"https://example.invalid/survey/{url_key}.htm"
        text = PHARMA.replace("3,400", f"{3_400 + 100 * version:,}")
        body = f'<div class="entry"><h1>合成・医薬品調査</h1><p>{text}</p></div>'.encode()
    else:
        url = f"https://www.boj.or.jp/mopo/mpmdeci/mpr_2026/{url_key}.htm"
        text = BOJ.replace("１．７５", ("１．７５", "１．５０", "１．２５")[version])
        body = f"<main><h1>金融市場調節方針</h1><p>{text}</p></main>".encode()
    if failed_at is not None:
        body = b"\xff"  # invalid UTF-8 under the recorded encoding
    receipt = ArticleReceipt(
        receipt_id=identity,
        source=source,
        requested_url=url,
        received_at=received_at,
        status="ok",
        http_status=200,
        body_ref=BodyRef(receipt_id=identity, sha256=sha256(body).hexdigest()),
    )
    retain_receipt(conn, receipt, body=body)
    run, observations = project_receipt(
        conn, identity, kind="document", generated_at=failed_at or received_at
    )
    if failed_at is not None:
        assert run.status == "parse_failed"
        assert not observations
        return None
    assert run.status == "parsed"
    (observation,) = observations
    return receipt, observation


def read_fixed(path, source, *, cutoff=CUTOFF):
    before = sha256(path.read_bytes()).digest()
    try:
        inputs = read_trend_inputs(
            path,
            release_id="synthetic",
            release_sha256="a" * 64,
            cutoff=cutoff,
            as_of=CUTOFF.date(),
        )
    finally:
        assert sha256(path.read_bytes()).digest() == before
    return inputs.social if source == "boj_news" else inputs.industries[0].inputs


@pytest.mark.parametrize(
    ("receipt_delta", "artifact_delta", "selected", "state"),
    [
        (0, 0, True, None),  # equivalent UTC/+09:00/-04:00 representations
        (-1, 1, False, "extraction_pending"),
        (1, 1, False, "not_collected"),
        (-1, 1000, False, "extraction_pending"),
        (1000, 1000, False, "not_collected"),
    ],
)
def test_fixed_reader_enforces_exact_receipt_and_artifact_cutoffs(
    source, tmp_path, receipt_delta, artifact_delta, selected, state
):
    path = tmp_path / "market.sqlite"
    received = (CUTOFF + receipt_delta * US).astimezone(EAST)
    generated = (CUTOFF + artifact_delta * US).astimezone(WEST)
    with closing(open_connection(path)) as conn, conn:
        _, observation = retain_document(conn, source, "receive", received_at=received)
        (artifact,) = extract_observations(
            conn, (observation.observation_id,), generated_at=generated
        )
        assert artifact.status == "extracted"
    fixed = read_fixed(path, source)
    assert fixed.coverage.latest_receipt_at == (received if receipt_delta <= 0 else None)
    assert fixed.coverage.source_states == (
        (f"{source}:ok:receipts=1",) if receipt_delta <= 0 else ()
    )
    if selected:
        assert [item.current.ref.artifact_id for item in fixed.artifacts] == [artifact.artifact_id]
    else:
        assert fixed.artifacts == ()
        assert fixed.coverage.state == state


def test_strict_previous_and_coverage_latest_respect_microseconds_and_equal_offsets(
    source, tmp_path
):
    path = tmp_path / "market.sqlite"
    old_time = CUTOFF - 20 * US
    current_time = CUTOFF - 10 * US
    with closing(open_connection(path)) as conn, conn:
        rows = []
        for label, version, stamp in (
            ("prior", 0, old_time.astimezone(EAST)),
            ("current-east", 1, current_time.astimezone(EAST)),
            ("current-west", 2, current_time.astimezone(WEST)),
        ):
            _, observation = retain_document(
                conn, source, label, received_at=stamp, version=version
            )
            (artifact,) = extract_observations(
                conn, (observation.observation_id,), generated_at=stamp + US
            )
            rows.append((observation, artifact))
    assert len({o.article_id for o, _ in rows}) == 1
    assert len({o.article_version_id for o, _ in rows}) == 3
    old, *equal_latest = rows
    newer_observation, newer_artifact = min(equal_latest, key=lambda row: row[0].observation_id)
    fixed = read_fixed(path, source)
    (pair,) = fixed.artifacts
    assert pair.current.ref.artifact_id == newer_artifact.artifact_id, (
        newer_observation.observation_id
    )
    assert fixed.coverage.latest_receipt_at == current_time
    assert pair.baseline is not None
    assert pair.baseline.ref.artifact_id == old[1].artifact_id
    assert fixed.manifest.baselines == (pair.baseline.ref,)


@pytest.mark.parametrize("side", ["current", "previous"])
def test_future_extractions_cannot_consume_the_65_row_sentinel(source, tmp_path, side):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn, conn:
        receipt, observation = retain_document(
            conn, source, "first", received_at=CUTOFF - timedelta(seconds=1)
        )
        (eligible,) = extract_observations(conn, (observation.observation_id,), generated_at=CUTOFF)
        # Create independently retained exact parser artifacts, as when combining
        # results from separate attempts. Only the clock is beyond this read.
        for offset in range(1, 66):
            save_artifact(
                conn, extract_observation(observation, receipt, generated_at=CUTOFF + offset * US)
            )
        if side == "previous":
            _, latest_obs = retain_document(
                conn, source, "third", version=1, received_at=CUTOFF - 10 * US
            )
            (latest,) = extract_observations(
                conn, (latest_obs.observation_id,), generated_at=CUTOFF
            )
        else:
            latest = eligible
    fixed = read_fixed(path, source)
    (pair,) = fixed.artifacts
    assert pair.current.ref.artifact_id == latest.artifact_id
    if side == "previous":
        assert pair.baseline is not None
        assert pair.baseline.ref.artifact_id == eligible.artifact_id
        assert not any(
            reason.startswith("baseline_candidate_limit:") for reason in fixed.coverage.reasons
        )
    else:
        # The exact same retained rows reach the cap when they actually become eligible.
        with pytest.raises(ValueError, match="too many competing extraction artifacts"):
            read_fixed(path, source, cutoff=CUTOFF + 65 * US)


def test_post_cutoff_receipts_cannot_displace_a_visible_article_before_the_article_cap(
    source, tmp_path
):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn, conn:
        _, visible = retain_document(
            conn, source, "visible", received_at=CUTOFF - timedelta(days=1)
        )
        (eligible,) = extract_observations(conn, (visible.observation_id,), generated_at=CUTOFF)
        for i in range(17):
            received = CUTOFF + (i + 1) * US
            _, future = retain_document(
                conn, source, f"later-{i}", received_at=received, url_key=f"later-{i:02d}"
            )
            extract_observations(conn, (future.observation_id,), generated_at=received)
    fixed = read_fixed(path, source)
    assert [item.current.ref.artifact_id for item in fixed.artifacts] == [eligible.artifact_id]
    assert fixed.coverage.omitted_artifacts == 0
    assert fixed.coverage.source_states == (f"{source}:ok:receipts=1",)


def test_failed_projection_only_replaces_current_after_its_own_clock(source, tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn, conn:
        _, visible = retain_document(conn, source, "valid", received_at=CUTOFF - 20 * US)
        (eligible,) = extract_observations(conn, (visible.observation_id,), generated_at=CUTOFF)
        retain_document(
            conn, source, "new-failed", received_at=CUTOFF - 10 * US, failed_at=CUTOFF + US
        )
    assert read_fixed(path, source).manifest.selected[0].artifact_id == eligible.artifact_id
    later = read_fixed(path, source, cutoff=CUTOFF + US)
    assert not later.artifacts
    assert "newer_document_projection_failed:new-failed" in later.coverage.reasons
    assert f"last_known_artifact:{eligible.artifact_id}" in later.coverage.reasons


def test_extraction_recovery_does_not_reuse_a_later_saved_artifact(source, tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn, conn:
        _, observation = retain_document(conn, source, "capture", received_at=CUTOFF - US)
        key = (observation.observation_id,)
        (future,) = extract_observations(conn, key, generated_at=CUTOFF + US)
        (fixed,) = extract_observations(conn, key, generated_at=CUTOFF)
        assert fixed.generated_at == CUTOFF
        assert fixed.artifact_id != future.artifact_id
        assert extract_observations(conn, key, generated_at=CUTOFF.astimezone(WEST)) == (fixed,)
        assert conn.execute("SELECT count(*) FROM article_extraction_artifacts").fetchone()[0] == 2


@pytest.mark.parametrize(
    "corrupt_clock",
    [
        "2026-10-25X11:00:00Z",
        "2026-10-25T12:39:00+00:99",
        "2026-W43-7T11:00:00Z",
        "2026-10-25T11:00:00",
        str((CUTOFF - timedelta(hours=1)).timestamp()),  # not wire-format ISO text
        None,
    ],
)
def test_invalid_saved_receipt_clock_does_not_win_coverage(source, tmp_path, corrupt_clock):
    path = tmp_path / "market.sqlite"
    known = (CUTOFF - timedelta(hours=2)).astimezone(timezone(timedelta(hours=5, minutes=45)))
    with closing(open_connection(path)) as conn, conn:
        for name, stamp in (("valid", known), ("damaged", CUTOFF - timedelta(hours=1))):
            retain_receipt(
                conn,
                ArticleReceipt(
                    receipt_id=name,
                    source=source,
                    requested_url="https://example.invalid/no-document",
                    received_at=stamp,
                    status="transport_error",
                    error_code="synthetic",
                ),
            )
        conn.execute(
            "UPDATE article_source_receipts "
            "SET receipt_json=json_set(receipt_json,'$.received_at',?) WHERE receipt_id='damaged'",
            (corrupt_clock,),
        )
    coverage = read_fixed(path, source).coverage
    assert coverage.latest_receipt_at == known
    assert coverage.latest_receipt_at.isoformat() == known.isoformat()
    assert coverage.source_states == (f"{source}:transport_error:receipts=1",)


def test_invalid_saved_artifact_clock_does_not_join_representation_candidates(source, tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn, conn:
        receipt, observation = retain_document(
            conn, source, "fixed", received_at=CUTOFF - timedelta(hours=1)
        )
        (eligible,) = extract_observations(conn, (observation.observation_id,), generated_at=CUTOFF)
        damaged = extract_observation(observation, receipt, generated_at=CUTOFF + US)
        save_artifact(conn, damaged)
        conn.execute(
            "UPDATE article_extraction_artifacts "
            "SET artifact_json=json_set(artifact_json,'$.generated_at',?) WHERE artifact_id=?",
            ("2026-10-25T12:39:00+00:99", damaged.artifact_id),
        )
    fixed = read_fixed(path, source)
    assert [item.current.ref.artifact_id for item in fixed.artifacts] == [eligible.artifact_id]


def test_existing_connection_can_match_twice_without_invalidating_a_stepped_cursor(
    tmp_path, source
):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn, conn:
        receipt, observation = retain_document(
            conn, source, "fixed", received_at=CUTOFF - timedelta(seconds=1)
        )
        for i in range(65):
            save_artifact(
                conn, extract_observation(observation, receipt, generated_at=CUTOFF - (64 - i) * US)
            )
    with (
        closing(connect_read_only(path)) as conn,
        closing(
            conn.execute(
                "SELECT artifact_id FROM article_extraction_artifacts ORDER BY artifact_id"
            )
        ) as unrelated,
    ):
        assert unrelated.fetchone() is not None
        first = matching_artifacts(conn, observation, cutoff=CUTOFF - US)
        assert len(first) == 64
        assert matching_artifacts(conn, observation, cutoff=CUTOFF - US) == first
        assert len(unrelated.fetchall()) == 64


@pytest.mark.parametrize(
    ("same_time", "genuinely_earlier"),
    [(1, 1), (64, 1), (1, 65)],
    ids=["one-same-time", "same-time-at-limit", "real-prior-overflow"],
)
def test_prior_artifact_uses_the_original_receipts_strict_bound_before_cap(
    source, tmp_path, same_time, genuinely_earlier
):
    path = tmp_path / "market.sqlite"
    current = CUTOFF - timedelta(seconds=1)
    older = current - timedelta(seconds=2)
    suffix = 2 if source == "tsr_data" else 1  # stable observation/artifact ID tie ordering
    with closing(open_connection(path)) as conn, conn:
        _, concurrent = retain_document(
            conn, source, f"new_B_{suffix}", version=1, received_at=current.astimezone(WEST)
        )
        _, latest = retain_document(
            conn, source, f"new_A_{suffix}", received_at=current.astimezone(EAST)
        )
        _, prior = retain_document(conn, source, f"old_B_{suffix}", version=1, received_at=older)
        assert latest.observation_id < concurrent.observation_id
        assert concurrent.article_version_id == prior.article_version_id
        assert latest.article_version_id != prior.article_version_id
        # Replay independent captures in descending generation order; receipt clocks remain
        # unchanged. A prior receipt's extraction may finish after the current receipt.
        previous_generated = current + 128 * US if same_time == 64 else older
        candidates = {}
        prior_capture = (prior, previous_generated, genuinely_earlier)
        equal_capture = (concurrent, current.astimezone(WEST), same_time)
        captures = (
            (prior_capture, equal_capture) if same_time == 64 else (equal_capture, prior_capture)
        )
        for observation, anchor, count in (*captures, (latest, current, 1)):
            artifacts = []
            for i in range(count, 0, -1):
                generated = anchor + i * US
                (item,) = extract_observations(
                    conn, (observation.observation_id,), generated_at=generated
                )
                assert item.observation_id == observation.observation_id
                assert item.generated_at == generated
                artifacts.append(item)
            candidates[observation.observation_id] = artifacts
    previous = candidates[prior.observation_id]
    equal_instant = candidates[concurrent.observation_id]
    (expected_current,) = candidates[latest.observation_id]
    if same_time == genuinely_earlier == 1:
        assert equal_instant[0].artifact_id < previous[0].artifact_id
    fixed = read_fixed(path, source)
    (selection,) = fixed.artifacts
    assert selection.current.ref.artifact_id == expected_current.artifact_id
    if genuinely_earlier == 65:
        assert selection.baseline is None
        assert f"baseline_candidate_limit:{prior.article_id}" in fixed.coverage.reasons
    else:
        assert selection.baseline is not None
        assert selection.baseline.created_at == previous[0].generated_at
        assert selection.baseline.evidence.received_at == older
        assert selection.baseline.ref.artifact_id == previous[0].artifact_id
