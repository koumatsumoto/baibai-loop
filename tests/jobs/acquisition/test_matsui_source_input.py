"""Synthetic Matsui current-page inputs; no provider or production store access."""

import json
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from tests.helpers.r2_store import MemoryR2Store

import baibai_engine.data_collection.acquisition.refresh as acquisition
import baibai_engine.data_collection.providers.matsui.margin as matsui
import baibai_engine.data_collection.receipts.native_sources.store as native_store
import baibai_jobs.acquisition.source_inputs as inputs
import baibai_jobs.integration.economic_handoff as handoff
from baibai_engine.data_collection.catalog import require_source
from baibai_engine.data_collection.receipts.native_sources.documents import (
    SourceDocument,
    store_document,
)
from baibai_engine.data_collection.receipts.native_sources.handoff import (
    accept_rechecks,
    export_receipts,
)
from baibai_engine.machine_analysis.observations.economic_indicators.service import (
    IndicatorsService,
)
from baibai_engine.machine_analysis.observations.economic_indicators.validation import (
    UnavailableObservationError,
)
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.acquisition.economic_staging import load_handoff
from baibai_jobs.integration.native_source_merge import merge_stores, verify_stores

SOURCE = "matsui-margin"
GROUP = "economic-matsui_margin"
SERIES = "jp.matsui_margin_long_evaluation_pnl"
DAY = date(2026, 11, 2)
WHEN = datetime(2026, 11, 2, 1, tzinfo=UTC)


def document(when=WHEN, value="-10", *, padding=0):
    receipt_day = when.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
    body = f"""<div class="market-brand-contents">
    <h3>信用残速報（松井証券内）</h3><p>11/2(月)</p>
    <h2>買い残</h2><dt>評価損益率</dt><dd>{value}％</dd><h2>倍率</h2>
    <p class="market-brand-contents-text">権利落ち日はみなし数値。</p></div>""".encode()
    return SourceDocument(
        SOURCE,
        receipt_day,
        require_source(SOURCE).source_url,
        "text/html",
        when,
        body + b" " * padding,
        {"receipt_jst_date": receipt_day},
    )


def save(path, *documents, status="ok"):
    with closing(native_store.initialize_database(path)) as conn, conn:
        for item in documents:
            store_document(conn, item)
            day = date.fromisoformat(item.request_key)
            native_store.record_source_run(
                conn,
                source_id=SOURCE,
                start=day,
                end=day,
                started_at=item.fetched_at,
                status=status,
                record_count=1,
            )


def publish(remote, database, identity, monkeypatch, *, legacy=False):
    with monkeypatch.context() as patch:
        if legacy:
            # The previous writer exported this group's entire receipt history.
            patch.setattr(
                inputs,
                "export_economic_receipts",
                lambda source, target, **kwargs: export_receipts(
                    source, target, source_ids=[SOURCE], since=kwargs["since"]
                ),
            )
        result = inputs.publish_inputs(
            remote,
            market=database.parent / "unused-market.sqlite",
            economic=database,
            identity=identity,
            asof=DAY,
            generations={"economic_etag": identity},
            only_group=GROUP,
        )
    assert result == {"updated": [GROUP], "failed": []}


def rows(path, table):
    with closing(native_store.open_read_only_connection(path)) as conn:
        return [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]  # nosec B608


def test_empty_input_reuses_bytes_preserves_canonical_and_reads_fixed_old_input(
    tmp_path, monkeypatch
):
    canonical = tmp_path / "canonical.sqlite"
    save(canonical, document(), document(WHEN + timedelta(days=1)), status="failed")
    original = canonical.read_bytes()
    remote = MemoryR2Store()
    publish(remote, canonical, "legacy", monkeypatch, legacy=True)
    old = tmp_path / "old.sqlite"
    inputs.load_input(remote, GROUP, old, tmp_path / "old-input")
    assert len(rows(old, "economic_source_documents")) == 2
    assert len(rows(old, "economic_source_runs")) == 2

    for identity in ("small1", "small2"):
        publish(remote, canonical, identity, monkeypatch)
        target = tmp_path / f"{identity}.sqlite"
        result = inputs.load_input(remote, GROUP, target, tmp_path / identity)
        assert result["canonical_generations"] == {"economic_etag": identity}
        for table in (*native_store.RECEIPT_KEYS, *native_store.CONFIRMATION_TABLES):
            assert rows(target, table) == []
    assert (tmp_path / "small1.sqlite").read_bytes() == (tmp_path / "small2.sqlite").read_bytes()
    assert len([key for key in remote.values if key.endswith(".sqlite")]) == 2
    assert canonical.read_bytes() == original
    # Advancing the mutable pointer does not change a previously fixed stage.
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
    with pytest.raises(LakePublishError, match="missing"):
        inputs.load_input(MemoryR2Store(), GROUP, tmp_path / "missing", tmp_path / "no-input")


def test_empty_seed_still_requires_a_current_canonical_schema(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical.sqlite"
    save(canonical, document())
    with closing(native_store.open_connection(canonical)) as conn, conn:
        conn.execute("PRAGMA user_version=12")
    remote = MemoryR2Store()
    with pytest.raises(native_store.EconomicSchemaError, match="expected 13"):
        publish(remote, canonical, "invalid", monkeypatch)
    assert remote.values == {}


@pytest.mark.parametrize("legacy", [False, True])
def test_current_get_rechecks_corrections_failure_and_l2_ignore_input_history(
    tmp_path, monkeypatch, legacy
):
    canonical = tmp_path / "canonical.sqlite"
    save(canonical, document())
    remote = MemoryR2Store()
    clock = WHEN
    payload = b""
    requests = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.astimezone(tz or UTC)

    def fetch(_session, url, **kwargs):
        requests.append(url)
        if payload is None:
            raise RuntimeError("synthetic provider failure")
        return payload

    def no_history(*_args, **_kwargs):
        raise AssertionError("Matsui acquisition enumerated old document history")

    for module in (acquisition, matsui, native_store, handoff):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(matsui, "fetch_bytes", fetch)
    monkeypatch.setattr(acquisition, "read_document_metadata", no_history)
    events = [
        (WHEN + timedelta(hours=1), "-10", 1, 0),  # same body
        (WHEN + timedelta(hours=2), "-20", 0, 0),  # A -> B
        (WHEN + timedelta(hours=3), "-10", 0, 0),  # B -> A
        (WHEN + timedelta(hours=2, minutes=30), "-30", 0, 1),  # delayed receipt
        (WHEN + timedelta(days=1), "-10", 0, 0),  # same body, different receipt day
        (WHEN + timedelta(days=1, hours=1), None, 0, 0),  # failed request
        (WHEN + timedelta(days=1, hours=2), "未公表", 0, 0),  # latest unreadable correction
    ]
    for index, (clock, value, unchanged, late) in enumerate(events):
        identity = f"cycle{index}"
        database, packet = tmp_path / f"{identity}.sqlite", tmp_path / f"packet{index}"
        if late:
            # This acquisition began from an older fixed input; its handoff
            # reached canonical only after the later correction was accepted.
            fixed_input = tmp_path / f"input{index}"
            load_handoff(
                remote,
                GROUP + "-cycle0",
                fixed_input,
                namespace="source-inputs",
                input_group=GROUP,
            )
            database.write_bytes((fixed_input / "receipts.sqlite").read_bytes())
        else:
            publish(remote, canonical, identity, monkeypatch, legacy=legacy)
            inputs.load_input(remote, GROUP, database, tmp_path / f"input{index}")
        payload = None if value is None else document(clock, value).payload
        assert handoff.acquire(
            "matsui_margin", "", DAY, packet, database, current_only_now=True
        ) == int(value is None)
        assert len(requests) == index + 1
        assert requests[-1] == require_source(SOURCE).source_url
        received = rows(packet / "receipts.sqlite", "economic_source_documents")
        assert len(received) == int(value is not None)
        if received:
            assert received[0][1] == clock.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
            assert received[0][2] == clock.isoformat(timespec="microseconds")
            assert received[0][5] == payload
        accepted = tmp_path / f"accepted{index}.sqlite"
        result = accept_rechecks(packet / "receipts.sqlite", canonical, accepted)
        assert result["unchanged_rechecks"] == unchanged
        assert len(result["late_recovery_required"]) == late
        merge_stores(accepted, canonical)
        verify_stores(accepted, canonical)
        report = json.loads((packet / "result.json").read_text())
        assert report["sources"][0]["history_recovery_required"] is False
        if value == "未公表":
            with pytest.raises(UnavailableObservationError):
                IndicatorsService(canonical).get_range(SERIES, start=DAY, end=DAY)
        else:
            observation = IndicatorsService(canonical).get_range(SERIES, start=DAY, end=DAY)
            assert observation.observations[0].value == (-20 if index == 1 else -10)
            # L2 is rebuilt from the fixed L1 copy, never the disposable input.
            fixed = tmp_path / f"fixed{index}.sqlite"
            fixed.write_bytes(canonical.read_bytes())
            assert IndicatorsService(fixed).get_range(SERIES, start=DAY, end=DAY) == observation
    assert len(rows(canonical, "economic_source_documents")) == 5
    assert [row[6] for row in rows(canonical, "economic_source_runs")].count("failed") == 1


def test_synthetic_input_transfer_and_retention_comparison(tmp_path, monkeypatch, capsys):
    results = {}
    for legacy in (True, False):
        directory = tmp_path / ("legacy" if legacy else "small")
        database = directory / "canonical.sqlite"
        save(
            database,
            *(document(WHEN - timedelta(days=offset), padding=8192) for offset in range(120)),
        )
        remote = MemoryR2Store()
        cycles = []
        for index in range(3):
            if index:
                save(database, document(WHEN + timedelta(days=index), padding=8192))
            remote.get_keys.clear()
            remote.put_keys.clear()
            remote.head_keys.clear()
            publish(remote, database, f"cycle{index}", monkeypatch, legacy=legacy)
            publication = {
                "get_count": len(remote.get_keys),
                "get_bytes": sum(len(remote.values[key].body) for key in remote.get_keys),
                "put_count": len(remote.put_keys),
                "put_bytes": sum(len(remote.values[key].body) for key in remote.put_keys),
                "head_count": len(remote.head_keys),
            }
            remote.get_keys.clear()
            remote.head_keys.clear()
            inputs.load_input(
                remote, GROUP, directory / f"copy{index}.sqlite", directory / f"input{index}"
            )
            cycles.append(
                {
                    "publish": publication,
                    "load_get_count": len(remote.get_keys),
                    "load_get_bytes": sum(len(remote.values[key].body) for key in remote.get_keys),
                    "load_head_count": len(remote.head_keys),
                }
            )
        results["legacy" if legacy else "small"] = {
            "cycles": cycles,
            "retained_bytes": sum(len(value.body) for value in remote.values.values()),
            "sqlite_count": sum(key.endswith(".sqlite") for key in remote.values),
        }
    old, new = results["legacy"], results["small"]
    assert old["sqlite_count"] == 3
    assert new["sqlite_count"] == 1
    assert new["retained_bytes"] < old["retained_bytes"] / 10
    for before, after in zip(old["cycles"], new["cycles"], strict=True):
        assert before["load_get_count"] == after["load_get_count"] == 3
        assert before["load_head_count"] == after["load_head_count"]
        assert before["publish"]["get_count"] == after["publish"]["get_count"]
        assert after["load_get_bytes"] < before["load_get_bytes"] / 10
    assert new["cycles"][-1]["publish"]["put_count"] == 2
    assert new["cycles"][-1]["publish"]["put_bytes"] < 2000
    with capsys.disabled():
        print("Synthetic Matsui input transfer comparison: " + json.dumps(results, sort_keys=True))
