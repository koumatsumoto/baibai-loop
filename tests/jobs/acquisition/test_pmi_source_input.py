"""PMI planning metadata is not a source document or an L2 input."""

import json
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from tests.helpers.r2_store import MemoryR2Store

import baibai_engine.data_collection.providers.spglobal.pmi as sp
import baibai_engine.data_collection.providers.tradingeconomics.pmi as te
import baibai_jobs.acquisition.source_inputs as inputs
from baibai_engine.data_collection.acquisition.policy import (
    _missing_ranges,
    acquisition_groups,
    explicit_recovery_ranges,
    recovery_ranges,
    source_policy,
)
from baibai_engine.data_collection.catalog import load_sources, require_source
from baibai_engine.data_collection.receipts.native_sources import store
from baibai_engine.data_collection.receipts.native_sources.documents import (
    PMI_INPUT_TABLE,
    SourceDocument,
    read_document_metadata,
    read_documents,
    store_document,
)
from baibai_engine.data_collection.receipts.native_sources.handoff import (
    accept_rechecks,
    export_pmi_input,
    export_receipts,
    validate_pmi_input,
)
from baibai_engine.data_collection.transport.context import (
    FetchContext,
    IndicatorsProviderError,
    SourceWithheldError,
)
from baibai_engine.machine_analysis.observations.economic_indicators.service import (
    IndicatorsService,
)
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.acquisition.economic_staging import load_handoff, save_handoff
from baibai_jobs.integration.native_source_merge import merge_stores, verify_stores

SOURCE = "spglobal-jp-pmi-mfg"
GROUP = "economic-spglobal_pmi"
MONTH = date(2026, 9, 1)
END = date(2026, 10, 29)
WHEN = datetime(2026, 10, 29, tzinfo=UTC)
URL = "https://www.pmi.spglobal.com/Public/Home/PressRelease/" + "a" * 32
TE_URL = "https://tradingeconomics.com/japan/manufacturing-pmi/news/100"


def pdf(*, month=MONTH, when=WHEN, payload=b"%PDF synthetic original", source=SOURCE):
    return SourceDocument(
        source,
        month.isoformat(),
        URL,
        "application/pdf",
        when,
        payload,
        {"observed_at": month.isoformat(), "release_observed_at": month.isoformat()},
    )


def save(path, *documents):
    with closing(store.initialize_database(path)) as conn, conn:
        for item in documents:
            store_document(conn, item)
            store.record_source_run(
                conn,
                source_id=item.source_id,
                start=date(2024, 1, 1),
                end=END,
                started_at=item.fetched_at,
                status="failed",
                record_count=1,
            )


def identities(path):
    with closing(store.open_read_only_connection(path)) as conn:
        return {item.request_key: item for item in read_document_metadata(conn, SOURCE)}


def publish(remote, path, identity, *, legacy=False):
    if legacy:

        def export_old(source, target, *, source_ids):
            export_receipts(
                source, target, source_ids=source_ids, since=datetime.min.replace(tzinfo=UTC)
            )

        def save_old(*args, **kwargs):
            # Same SQLite envelope as the previous writer. These legacy test
            # cycles all change bytes, so none reuses a prior receipt blob.
            kwargs.update(input_group=None, reusable_receipt=None)
            return save_handoff(*args, **kwargs)

        with (
            patch.object(inputs, "export_pmi_input", export_old),
            patch.object(inputs, "save_handoff", save_old),
        ):
            publish(remote, path, identity)
        return
    assert inputs.publish_inputs(
        remote,
        market=path.parent / "unused-market",
        economic=path,
        identity=identity,
        asof=END,
        generations={"economic_etag": identity},
        only_group=GROUP,
    ) == {"updated": [GROUP], "failed": []}


def test_metadata_preserves_latest_identity_and_recovery_without_fake_receipts(tmp_path):
    canonical = tmp_path / "canonical.sqlite"
    old = pdf(when=WHEN - timedelta(days=1))
    changed = replace(
        pdf(), source_url=URL.replace("a", "b"), metadata={**old.metadata, "extra": None}
    )
    flash = replace(
        pdf(month=date(2026, 8, 1)), metadata={"observed_at": "2026-08-01", "phase": "flash"}
    )
    unresolved = replace(
        pdf(month=date(2026, 7, 1)),
        metadata={"requested_observed_at": "2026-07-01", "identity_error": "unresolved"},
        content_type="text/html",
    )
    save(canonical, old, changed, flash, unresolved)
    before = canonical.read_bytes()
    seed = tmp_path / "input.sqlite"
    export_pmi_input(canonical, seed, source_ids=[SOURCE])
    validate_pmi_input(seed)
    assert identities(seed) == identities(canonical)
    assert all(not hasattr(item, "payload") for item in identities(seed).values())
    assert b"synthetic original" not in seed.read_bytes()
    plans = []
    for path in (canonical, seed):
        with closing(store.open_read_only_connection(path)) as conn:
            plans.append(
                (
                    recovery_ranges(conn, require_source(SOURCE), END),
                    explicit_recovery_ranges(conn, require_source(SOURCE), END),
                )
            )
            if path == seed:
                assert list(read_documents(conn, SOURCE)) == []
    assert plans[0] == plans[1]
    assert canonical.read_bytes() == before
    new = replace(changed, fetched_at=WHEN + timedelta(seconds=1), metadata=old.metadata)
    with closing(store.open_connection(seed)) as conn, conn:
        store_document(conn, new)
    assert identities(seed)[new.request_key].metadata == old.metadata
    stage = tmp_path / "actual.sqlite"
    export_receipts(seed, stage, source_ids=[SOURCE], since=new.fetched_at)
    with closing(store.open_read_only_connection(stage)) as conn:
        assert list(read_documents(conn, SOURCE)) == [new]
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (PMI_INPUT_TABLE,)
        ).fetchone()
    accepted = tmp_path / "accepted.sqlite"
    assert accept_rechecks(stage, canonical, accepted)["late_recovery_required"] == []
    merge_stores(accepted, canonical)
    verify_stores(accepted, canonical)


def test_new_reader_accepts_old_input_and_new_format_is_input_only(tmp_path):
    canonical = tmp_path / "canonical.sqlite"
    save(canonical, pdf())
    remote = MemoryR2Store()
    publish(remote, canonical, "legacy", legacy=True)
    old = tmp_path / "old.sqlite"
    inputs.load_input(remote, GROUP, old, tmp_path / "old")
    assert identities(old) == identities(canonical)
    for identity in ("small1", "small2"):
        publish(remote, canonical, identity)
        target = tmp_path / f"{identity}.sqlite"
        report = inputs.load_input(remote, GROUP, target, tmp_path / identity)
        assert report["canonical_generations"] == {"economic_etag": identity}
        validate_pmi_input(target)
        assert identities(target) == identities(old)
    assert len([key for key in remote.values if key.endswith(".sqlite")]) == 2
    manifest = json.loads(
        remote.values[f"coordination/source-inputs/{GROUP}-small2/handoff.json"].body
    )
    assert manifest["receipt_format"] == "pmi-acquisition-input-v1"
    assert "planning_key" in manifest
    assert "receipt_key" not in manifest
    # A legacy handoff reader requires receipt_key, so it fails before a GET plan.
    remote.store_object(
        "coordination/economic-sources/fake/handoff.json", json.dumps(manifest).encode()
    )
    with pytest.raises(ValueError, match="not a source receipt handoff"):
        load_handoff(remote, "fake", tmp_path / "fake")
    load_handoff(
        remote,
        GROUP + "-legacy",
        tmp_path / "fixed-old",
        namespace="source-inputs",
        input_group=GROUP,
    )
    assert (tmp_path / "fixed-old/receipts.sqlite").read_bytes() == old.read_bytes()
    pointer = json.loads(remote.values[inputs.input_key(GROUP)].body)
    pointer["canonical_generations"] = {"economic_etag": "different"}
    remote.store_object(inputs.input_key(GROUP), json.dumps(pointer).encode())
    with pytest.raises(LakePublishError, match="generation binding"):
        inputs.load_input(remote, GROUP, tmp_path / "wrong.sqlite", tmp_path / "wrong")
    with pytest.raises(ValueError, match="metadata table"):
        validate_pmi_input(old)


@pytest.mark.parametrize(
    "case", ["matching", "url", "type", "month", "recheck", "force", "403", "partial"]
)
def test_pdf_acquisition_decisions_match_full_body_input(tmp_path, monkeypatch, case):
    stored = pdf()
    if case == "url":
        stored = replace(stored, source_url=URL + "changed")
    if case == "type":
        stored = replace(stored, content_type="text/html")
    if case == "month":
        stored = replace(stored, metadata={**stored.metadata, "release_observed_at": "2026-10-01"})
    canonical, seed = tmp_path / "canonical.sqlite", tmp_path / "seed.sqlite"
    save(canonical, stored)
    export_pmi_input(canonical, seed, source_ids=[SOURCE])
    release = sp.Release(MONTH, MONTH, URL)
    august = sp.Release(date(2026, 8, 1), date(2026, 8, 1), URL + "-unavailable")
    releases = (august, release) if case == "partial" else (release,)
    monkeypatch.setattr(sp, "load_manifest", lambda _: {"jp_manufacturing": releases})
    results = []
    for path in (canonical, seed):
        calls, received, errors = [], [], []

        def fetch(url, _calls=calls, **kwargs):
            _calls.append(url)
            if case == "403":
                raise SourceWithheldError("synthetic HTTP 403")
            if case == "partial" and url == august.url:
                raise IndicatorsProviderError("synthetic unavailable PDF")
            return b"%PDF corrected original"

        monkeypatch.setattr(sp, "fetch_pdf_bytes", fetch)
        with FetchContext() as context:
            try:
                received.extend(
                    sp.SpGlobalPmiSource().fetch_documents(
                        require_source(SOURCE),
                        start=august.observed_at if case == "partial" else MONTH,
                        end=END,
                        session=context.session,
                        context=context,
                        existing=identities(path),
                        force=case == "force",
                        recheck_existing=case in {"recheck", "403", "partial"},
                    )
                )
            except IndicatorsProviderError as error:
                errors.append((type(error), str(error)))
        results.append((calls, [item.payload for item in received], errors))
    assert results[0] == results[1]
    assert len(results[1][0]) == (2 if case == "partial" else int(case != "matching"))
    if case in {"403", "partial"}:
        assert results[1][2]
    if case == "partial":
        assert results[1][1] == [b"%PDF corrected original"]


def html(lead, *, published="2026-10-01T00:30:00Z"):
    metadata = {
        "@type": "Article",
        "url": TE_URL,
        "datePublished": published,
        "dateModified": published,
    }
    return (
        f'<div class="news_description"><p>{lead}</p></div>'
        f'<script type="application/ld+json">{json.dumps(metadata)}</script>'
    ).encode()


@pytest.mark.parametrize("case", ["final", "flash", "forecast", "unpublished", "moved-month"])
def test_te_identity_actual_forecast_and_unpublished_are_not_reinterpreted(
    tmp_path, monkeypatch, case
):
    source = replace(require_source(SOURCE), provider="tradingeconomics_pmi")
    prior = SourceDocument(
        SOURCE,
        "te:100",
        TE_URL,
        "text/html",
        WHEN,
        b"old complete HTML",
        {
            "delivery_provider": "tradingeconomics",
            "measurement_provider": "spglobal",
            "stream": "jp_manufacturing",
            "observed_at": MONTH.isoformat(),
            "phase": "final",
            "published_at": "2026-10-01T00:30:00+00:00",
            "modified_at": "2026-10-01T00:30:00+00:00",
        },
    )
    canonical, seed = tmp_path / "canonical.sqlite", tmp_path / "seed.sqlite"
    save(canonical, prior)
    export_pmi_input(canonical, seed, source_ids=[SOURCE])
    lead = "S&P Global Japan Manufacturing PMI was confirmed at 54.1 in September 2026, above a forecast of 53.0."
    if case == "flash":
        lead = "S&P Global Japan Manufacturing PMI rose to 52.7 in September 2026, according to a flash estimate."
    elif case == "forecast":
        lead = "S&P Global Japan Manufacturing PMI is forecast at 53.0 in September 2026."
    elif case == "unpublished":
        lead = "The result is not yet published."
    elif case == "moved-month":
        lead = lead.replace("September", "August")
    payload = html(
        lead, published="2026-09-24T00:30:00Z" if case == "flash" else "2026-10-01T00:30:00Z"
    )
    comparisons = []
    for path in (canonical, seed):
        calls, received, errors = [], [], []

        def fetch(_session, url, _calls=calls, **kwargs):
            _calls.append(url)
            return payload

        monkeypatch.setattr(te, "fetch_bytes", fetch)
        with FetchContext() as context:
            try:
                received.extend(
                    te.TradingEconomicsPmiSource().fetch_documents(
                        source,
                        start=MONTH,
                        end=END,
                        session=context.session,
                        context=context,
                        existing=identities(path),
                        force=False,
                        selected_urls=(TE_URL,),
                    )
                )
            except IndicatorsProviderError as error:
                errors.append(str(error))
        comparisons.append((calls, [dict(item.metadata) for item in received], errors))
        assert received[0].payload == payload
        fresh = replace(received[0], fetched_at=WHEN + timedelta(seconds=1))
        with closing(store.open_connection(path)) as conn, conn:
            store_document(conn, fresh)
            missing = _missing_ranges(conn, source, MONTH, date(2026, 9, 30), [])
        assert bool(missing) == (case != "final")
        if path == seed:
            stage = tmp_path / "stage.sqlite"
            export_receipts(path, stage, source_ids=[SOURCE], since=fresh.fetched_at)
            # The staged object still contains real bytes; metadata is never L2.
            if case == "final":
                value = IndicatorsService(stage).get_range(
                    "jp.pmi_manufacturing", start=MONTH, end=MONTH
                )
                assert [item.value for item in value.observations] == [54.1]
                assert "final" in value.observations[0].quality_flags
            elif case in {"forecast", "unpublished"}:
                with pytest.raises(ValueError, match="main article"):
                    IndicatorsService(stage).get_range(
                        "jp.pmi_manufacturing", start=MONTH, end=MONTH
                    )
    assert comparisons[0] == comparisons[1]
    if case in {"forecast", "unpublished"}:
        assert "identity_error" in comparisons[1][1][0]
        assert comparisons[1][1][0]["requested_observed_at"] == MONTH.isoformat()


def test_other_eleven_groups_keep_their_exact_sqlite_input(tmp_path):
    canonical = tmp_path / "canonical.sqlite"
    for source in load_sources().values():
        save(canonical, pdf(source=source.source_id))
    remote = MemoryR2Store()
    for group in acquisition_groups():
        # The combined rollout gives these four groups explicit source-specific
        # contracts. The all-group integration test covers their dispatch.
        if group in {"spglobal_pmi", "matsui_margin", "tsr_bankruptcies", "fred_csv"}:
            continue
        name = "economic-" + group
        assert (
            inputs.publish_inputs(
                remote,
                market=tmp_path / "unused",
                economic=canonical,
                identity="test",
                asof=END,
                generations={"economic_etag": "fixed"},
                only_group=name,
            )["failed"]
            == []
        )
        expected = tmp_path / f"expected-{group}.sqlite"
        export_receipts(
            canonical,
            expected,
            source_ids=sorted(
                s.source_id
                for s in load_sources().values()
                if source_policy(s).acquisition_group == group
            ),
            since=datetime.min.replace(tzinfo=UTC),
        )
        actual = tmp_path / f"actual-{group}.sqlite"
        inputs.load_input(remote, name, actual, tmp_path / group)
        assert actual.read_bytes() == expected.read_bytes()


def test_synthetic_pdf_history_reduction_does_not_add_gets(tmp_path, capsys):
    comparisons = {}
    for legacy in (True, False):
        root = tmp_path / ("legacy" if legacy else "metadata")
        canonical = root / "canonical.sqlite"
        for index in range(36):
            month = date(2023 + index // 12, 1 + index % 12, 1)
            for revision in range(2):
                save(
                    canonical,
                    pdf(
                        month=month,
                        when=WHEN - timedelta(days=80 - index * 2 - revision),
                        payload=b"%PDF original " + bytes([65 + revision]) * 32768,
                    ),
                )
        remote = MemoryR2Store()
        cycles = []
        for index in range(3):
            if index:
                save(
                    canonical,
                    pdf(
                        when=WHEN + timedelta(seconds=index),
                        payload=b"%PDF correction " + bytes([65 + index]) * 32768,
                    ),
                )
            remote.get_keys.clear()
            remote.put_keys.clear()
            publish(remote, canonical, f"cycle{index}", legacy=legacy)
            publication = {
                "get_count": len(remote.get_keys),
                "put_count": len(remote.put_keys),
                "put_bytes": sum(len(remote.values[key].body) for key in remote.put_keys),
            }
            remote.get_keys.clear()
            inputs.load_input(remote, GROUP, root / f"copy{index}.sqlite", root / f"input{index}")
            cycles.append(
                {
                    "publish": publication,
                    "load_get_count": len(remote.get_keys),
                    "load_get_bytes": sum(len(remote.values[key].body) for key in remote.get_keys),
                }
            )
        comparisons["legacy" if legacy else "metadata"] = {
            "cycles": cycles,
            "retained_bytes": sum(len(value.body) for value in remote.values.values()),
        }
    old, new = comparisons["legacy"], comparisons["metadata"]
    assert new["retained_bytes"] < old["retained_bytes"] / 10
    for before, after in zip(old["cycles"], new["cycles"], strict=True):
        assert before["load_get_count"] == after["load_get_count"] == 3
        assert before["publish"]["get_count"] == after["publish"]["get_count"]
        assert before["publish"]["put_count"] == after["publish"]["put_count"] == 3
        assert after["load_get_bytes"] < before["load_get_bytes"] / 10
    with capsys.disabled():
        print("Synthetic PMI input transfer comparison: " + json.dumps(comparisons, sort_keys=True))
