"""Invalid EDINET originals stay durable without replacing the serving source view."""

import hashlib
import json
import shutil
from contextlib import closing
from datetime import timedelta

import pytest
from tests.engine.test_edinet_handoff import DAY, STAMP, projection_state, receive
from tests.helpers.jquants_receipts import calendar
from tests.helpers.lake_policy import narrow_release_policy
from tests.jobs.test_source_receipts import Store

from baibai_engine.data_collection.providers.edinet.receipts import retain_list_response
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.machine_analysis.observations.disclosures.edinet_document_store import (
    read_observed_documents,
    read_stored_documents,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_list_rebuild import (
    rebuild_edinet_document_day,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.store import (
    read_edinet_documents,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.hydrate import dehydrate_market_store, hydrate_market_store
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import resolve_release
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.acquisition.receipts import available_handoffs, seal_integrated
from baibai_jobs.integration.source_handoff import _export
from baibai_jobs.integration.sources import apply, main
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.policy import release_policy_for_profile


@pytest.mark.parametrize(
    ("new_day", "other_partial"),
    [(False, False), (True, False), (True, True)],
    ids=["preserved-old-day", "partial-new-day", "other-source-still-strict"],
)
def test_edinet_failure_is_publishable_but_remains_unreadable_and_reported_after_hydrate(
    tmp_path, monkeypatch, new_day, other_partial
):
    store = Store()
    market, economic = tmp_path / "market.sqlite", tmp_path / "economic.sqlite"
    receive(market, "S100OLD", STAMP)
    rebuild_edinet_document_day(market, DAY)
    prior = projection_state(market)
    with closing(initialize_database(economic)):
        pass
    bad_source = tmp_path / "bad.sqlite"
    bad_day = DAY + timedelta(days=int(new_day))
    with closing(open_connection(bad_source)) as conn:
        bad_receipt = retain_list_response(
            conn,
            doc_date=bad_day,
            received_at=STAMP + timedelta(days=1, hours=1),
            body=b"{invalid JSON",
        )
    asof = bad_day + timedelta(days=1)
    if new_day:
        receive(bad_source, "S100GOOD", STAMP + timedelta(days=2), day=asof)
    bad_packet = tmp_path / "bad-packet"
    _export("edinet-list", asof, STAMP, bad_source, bad_packet)
    save_handoff(store, "edinet-list-bad", bad_packet, market=True)
    good_source = tmp_path / "good.sqlite"
    calendar(good_source, DAY.isoformat(), asof.isoformat(), 1, STAMP + timedelta(hours=2))
    good_packet = tmp_path / "good-packet"
    _export("jquants-market", asof, STAMP, good_source, good_packet)
    save_handoff(store, "jquants-market-good", good_packet, market=True)
    work = tmp_path / "integration"
    work.mkdir()
    (work / "plan.json").write_text(json.dumps({"handoffs": available_handoffs(store)}))
    report = apply(store, work, market=market, economic=economic, asof=asof)
    outcomes = {item["entry"]["source"]: item for item in report["handoffs"]}
    assert outcomes["edinet-list"]["applied"] is True
    assert outcomes["edinet-list"]["failed"] is True
    assert outcomes["edinet-list"]["recovery"] is True
    assert outcomes["edinet-list"]["result"]["failed_days"] == [bad_day.isoformat()]
    assert outcomes["jquants-market"]["failed"] is False
    expected = projection_state(market)
    if new_day:
        assert all(actual[0] == old[0] for actual, old in zip(expected, prior, strict=True))
        assert read_edinet_documents(market, bad_day) is None
        assert [item["docID"] for item in read_edinet_documents(market, asof)] == ["S100GOOD"]
        with closing(open_connection(market)) as conn:
            with pytest.raises(
                EDINETProviderError, match=f"coverage failed: {bad_day.isoformat()}"
            ):
                read_stored_documents(conn, through=asof)
            assert [item["docID"] for item in read_observed_documents(conn, through=asof)] == [
                "S100OLD",
                "S100GOOD",
            ]
    else:
        assert expected == prior
    with closing(open_connection(market)) as conn:
        assert conn.execute(
            "SELECT receipt_id FROM edinet_list_responses WHERE receipt_id = ?", (bad_receipt,)
        ).fetchone() == (bad_receipt,)
        assert conn.execute("SELECT day FROM jquants_market_calendar ORDER BY day").fetchall() == [
            ((DAY + timedelta(days=offset)).isoformat(),) for offset in range((asof - DAY).days + 1)
        ]
    assert main(["check", "--directory", str(work)]) == 1

    names = (
        "edinet.documents",
        "edinet.document_lists",
        "edinet.list_responses",
        "jquants.market_calendar",
        "jquants.source_pages",
    )
    narrow_release_policy(monkeypatch, datasets=names, carries_history=None)
    policy = release_policy_for_profile("production")
    policies = {item.dataset: item for item in policy.datasets}
    assert policies["edinet.documents"].required is True
    assert policies["edinet.documents"].carries_history is True
    assert policies["edinet.documents"].require_complete_coverage is False
    assert policies["jquants.market_calendar"].require_complete_coverage is True
    if other_partial:
        with closing(open_connection(market)) as conn, conn:
            conn.execute(
                "UPDATE source_coverage SET status = 'partial', error = 'synthetic rejected calendar' "
                "WHERE source = 'jquants_market_calendar'"
            )
    mirror = tmp_path / "lake"
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=mirror) as snapshot:
        exports = [
            export_dataset_snapshot(
                dataset=LAKE_DATASETS[name],
                mirror_root=mirror,
                source_snapshot=snapshot,
                producer_git_commit="a" * 40,
                build_id=name.replace(".", "-"),
            )
            for name in names
        ]
    assert next(
        item.manifest for item in exports if item.manifest.dataset == "edinet.documents"
    ).coverage_status == ("partial" if new_day else "complete")
    if other_partial:
        with pytest.raises(ValueError, match=r"jquants.market_calendar: coverage is partial"):
            create_release(
                dataset_manifest_paths=[item.manifest_path for item in exports],
                mirror_root=mirror,
                release_id="other-source-incomplete",
                catalog=LAKE_DATASETS,
                policy=policy,
            )
        return
    manifest_path, release = create_release(
        dataset_manifest_paths=[item.manifest_path for item in exports],
        mirror_root=mirror,
        release_id="edinet-source-isolation",
        catalog=LAKE_DATASETS,
        policy=policy,
    )
    source = LocalMirrorSource(mirror)
    fixed = resolve_release(
        source,
        release.release_id,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
    restored = tmp_path / "restored.sqlite"
    shutil.copyfile(market, restored)
    cache = LakeObjectCache(root=mirror, source=source)
    with lake_session() as session:
        hydrate_market_store(
            session,
            release=fixed,
            cache=cache,
            store=restored,
            dataset_names=names,
            catalog=LAKE_DATASETS,
        )
        dehydrate_market_store(restored, release=fixed, catalog=LAKE_DATASETS)
        with closing(open_connection(restored)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM edinet_list_responses").fetchone() == (0,)
            assert conn.execute("SELECT COUNT(*) FROM edinet_document_lists").fetchone() == (0,)
        hydrate_market_store(
            session,
            release=fixed,
            cache=cache,
            store=restored,
            dataset_names=names,
            catalog=LAKE_DATASETS,
        )
    assert projection_state(restored) == expected
    if new_day:
        assert read_edinet_documents(restored, bad_day) is None
        assert [item["docID"] for item in read_edinet_documents(restored, asof)] == ["S100GOOD"]
        with closing(open_connection(restored)) as conn:
            with pytest.raises(
                EDINETProviderError, match=f"coverage failed: {bad_day.isoformat()}"
            ):
                read_stored_documents(conn, through=asof)
            assert [item["docID"] for item in read_observed_documents(conn, through=asof)] == [
                "S100OLD",
                "S100GOOD",
            ]
    else:
        assert [item["docID"] for item in read_edinet_documents(restored, bad_day)] == ["S100OLD"]
    for outcome in report["handoffs"]:
        seal_integrated(store, outcome["entry"], generations={}, recovery=outcome["recovery"])
    assert available_handoffs(store) == []
    resume = tmp_path / "no-new-handoff"
    resume.mkdir()
    (resume / "plan.json").write_text(json.dumps({"handoffs": []}))
    resumed = apply(store, resume, market=restored, economic=economic, asof=asof)
    assert resumed["handoffs"] == []
    assert resumed["extractions"]["edinet-list"]["failed_days"] == [bad_day.isoformat()]
    assert (
        resumed["extractions"]["edinet-metrics"]["input_asof"]
        == (asof if new_day else DAY).isoformat()
    )
    assert main(["check", "--directory", str(resume)]) == 1
