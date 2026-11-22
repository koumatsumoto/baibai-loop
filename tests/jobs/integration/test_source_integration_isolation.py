import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta

import pytest
from tests.helpers.edinet_native import empty_edinet_input
from tests.helpers.jquants_receipts import FIRST_AT
from tests.helpers.master_calendar_native import seed_calendar, seed_master
from tests.helpers.native_handoff import integrate_economic, stage
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.receipts.native_sources.records import read_snapshots
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError, R2Error
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.acquisition.receipts import available_handoffs, seal_integrated
from baibai_jobs.integration.replay_result import ReplayResult
from baibai_jobs.integration.sources import apply as apply_market


def native_dependencies(store, market, work, asof):
    from datetime import UTC, datetime

    from baibai_jobs.integration.calendar_native import model_input

    calendar = seed_calendar(store, market, work / "fixture-calendar", as_of=asof)
    seed_master(store, market, work / "fixture-master", as_of=asof)
    model_input(store, calendar.fixed(), as_of=asof, evaluated_at=datetime.now(UTC), market=market)


def apply(store, work, *, market, economic, asof):
    native_dependencies(store, market, work, asof)
    economic_report = integrate_economic(store, work / "economic", economic, asof)
    plan = json.loads((work / "plan.json").read_text())
    plan["handoffs"] = [entry for entry in plan["handoffs"] if entry["market"]]
    (work / "plan.json").write_text(json.dumps(plan))
    result = apply_market(store, work, market=market, asof=asof)
    result["handoffs"].extend(economic_report["handoffs"])
    return result


@pytest.mark.parametrize("projection_failed", [False, True])
def test_incomplete_market_projection_preserves_explicit_recovery_and_watermark(
    tmp_path, monkeypatch, projection_failed
):
    store = MemoryR2Store()
    saved_edinet = empty_edinet_input(store, tmp_path)
    from tests.helpers.native_margin_sources import initialize_margin_sources

    initialize_margin_sources(store, tmp_path / "empty-margin-baseline")
    from baibai_engine.data_collection.providers.jquants.price_native import PriceL1

    PriceL1(store, tmp_path / "empty-price-baseline").initialize()
    market, economic = tmp_path / "market.sqlite", tmp_path / "economic.sqlite"
    for path, initialize in ((market, open_connection), (economic, initialize_database)):
        with closing(initialize(path)):
            pass
    packet = tmp_path / "packet"
    packet.mkdir()
    with closing(open_connection(packet / "receipts.sqlite")):
        pass
    report = {
        "source": "jpx-regulations",
        "asof": "2026-10-24",
        "started_at": FIRST_AT.isoformat(),
    }
    (packet / "result.json").write_text(json.dumps(report))
    save_handoff(store, "jpx-partial", packet, market=True)
    work = tmp_path / "integration"
    work.mkdir()
    (work / "plan.json").write_text(
        json.dumps(
            {
                "handoffs": available_handoffs(store),
                "edinet_input": saved_edinet.model_dump(mode="json"),
            }
        )
    )
    monkeypatch.setattr(
        "baibai_jobs.integration.sources._replay",
        lambda *args, **kwargs: ReplayResult(
            report={"projection_failures": ["bundle"] if projection_failed else []},
            complete=not projection_failed,
        ),
    )
    monkeypatch.setattr(
        "baibai_jobs.integration.sources.extract_saved_edinet", lambda *args, **kwargs: {}
    )
    outcome = apply(store, work, market=market, economic=economic, asof=date(2026, 10, 24))[
        "handoffs"
    ][0]
    assert outcome["applied"] is True
    assert outcome["failed"] is projection_failed
    assert outcome["recovery"] is projection_failed
    seal_integrated(store, outcome["entry"], generations={}, recovery=outcome["recovery"])
    assert available_handoffs(store) == []
    explicit = available_handoffs(store, include_integrated=True)
    assert explicit[0]["integration"]["status"] == (
        "needs_explicit_recovery" if projection_failed else "integrated"
    )
    report["started_at"] = (FIRST_AT - timedelta(hours=1)).isoformat()
    (packet / "result.json").write_text(json.dumps(report))
    save_handoff(store, "jpx-earlier", packet, market=True)
    assert available_handoffs(store)[0]["late_before_integrated"] is (not projection_failed)


def _mixed_handoffs(tmp_path, monkeypatch, *, corrupt_market=False, market_report=None):
    store = MemoryR2Store()
    saved_edinet = empty_edinet_input(store, tmp_path)
    from tests.helpers.native_margin_sources import initialize_margin_sources

    initialize_margin_sources(store, tmp_path / "empty-margin-baseline")
    from baibai_engine.data_collection.providers.jquants.price_native import PriceL1

    PriceL1(store, tmp_path / "empty-price-baseline").initialize()
    market, economic = tmp_path / "market.sqlite", tmp_path / "economic.sqlite"
    for path, initialize in ((market, open_connection), (economic, initialize_database)):
        with closing(initialize(path)):
            pass
    packet = tmp_path / "market-packet"
    packet.mkdir()
    if corrupt_market:
        (packet / "receipts.sqlite").write_bytes(b"not a SQLite receipt")
    else:
        with closing(open_connection(packet / "receipts.sqlite")):
            pass
    (packet / "result.json").write_text(
        json.dumps(
            market_report
            or {
                "source": "jpx-regulations",
                "asof": "2026-10-24",
                "started_at": FIRST_AT.isoformat(),
                "bundle_ids": [],
            }
        )
    )
    save_handoff(store, "jpx-regulations-fault", packet, market=True)
    stage(store, tmp_path / "good", "fred_csv-good", "fred_csv", value="independent receipt")
    entries = available_handoffs(store)
    entries.sort(key=lambda entry: not entry["market"])
    work = tmp_path / "integration"
    work.mkdir()
    (work / "plan.json").write_text(
        json.dumps({"handoffs": entries, "edinet_input": saved_edinet.model_dump(mode="json")})
    )
    monkeypatch.setattr(
        "baibai_jobs.integration.sources.extract_saved_edinet", lambda *args, **kwargs: {}
    )
    return store, work, market, economic


@pytest.mark.parametrize(
    ("operation", "error", "fatal"),
    [
        ("download_fixed", R2Error("transport unavailable"), True),
        ("download_fixed", LakeCASConflict("conditional generation changed"), True),
        ("download_fixed", LakePublishError("R2 is only source-contract text"), False),
        ("_replay", sqlite3.DatabaseError("canonical corruption"), True),
        ("_replay", sqlite3.OperationalError("canonical storage unavailable"), True),
        ("_replay", OSError("canonical I/O failed"), True),
        ("_replay", sqlite3.IntegrityError("source identity conflicts"), False),
    ],
)
def test_apply_preserves_global_and_source_local_error_scope(
    tmp_path, monkeypatch, operation, error, fatal
):
    import baibai_jobs.integration.sources as integration

    store, work, market, economic = _mixed_handoffs(tmp_path, monkeypatch)
    original = getattr(integration, operation)

    def fail_market(*args, **kwargs):
        if operation == "_replay" or args[1]["market"]:
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(integration, operation, fail_market)
    if fatal:
        with pytest.raises(type(error)):
            apply(store, work, market=market, economic=economic, asof=date(2026, 10, 24))
        assert not (work / "applied.json").exists()
    else:
        report = apply(store, work, market=market, economic=economic, asof=date(2026, 10, 24))
        assert report["handoffs"][0]["result"] == {"reason": type(error).__name__}
        assert report["handoffs"][0]["failed"] is True
        assert report["handoffs"][1]["failed"] is False
        with closing(initialize_database(economic)) as conn:
            assert next(
                read_snapshots(
                    conn, date(2026, 10, 24), date(2026, 10, 24), source_id="fred-payems"
                )
            ).records == ({"value": "independent receipt"},)


def test_invalid_receipt_sqlite_is_local_and_other_source_still_integrates(tmp_path, monkeypatch):
    store, work, market, economic = _mixed_handoffs(tmp_path, monkeypatch, corrupt_market=True)
    report = apply(store, work, market=market, economic=economic, asof=date(2026, 10, 24))
    assert report["handoffs"][0]["result"] == {"reason": "ValueError"}
    assert report["handoffs"][0]["failed"] is True
    assert report["handoffs"][1]["failed"] is False


@pytest.mark.parametrize(
    ("empty", "malformed", "fault"),
    [
        ("calendar", None, None),
        ("master", None, None),
        (None, "calendar", "clock"),
        (None, "master", "clock"),
        ("calendar", "master", "clock"),
        ("master", "calendar", "clock"),
        ("calendar", "master", "shape"),
        ("master", "calendar", "shape"),
        (None, "calendar", "older-pack"),
        (None, "calendar", "required-pack"),
        (None, "calendar", "required-transport"),
    ],
)
def test_tradingview_calendar_keeps_source_local_scope_and_fatal_root_checks(
    tmp_path, monkeypatch, empty, malformed, fault
):
    from tests.helpers.jpx_earnings import DAY as JPX_DAY
    from tests.helpers.jpx_earnings import STAMP, earnings
    from tests.helpers.tradingview import (
        DAY,
        NOW,
        _sdk,
        native_inputs,
        payload,
        retain_result,
        store,
    )

    from baibai_engine.data_collection.providers.jquants.calendar_native import HEAD_KEY as CALENDAR
    from baibai_engine.data_collection.providers.jquants.master_native import HEAD_KEY as MASTER
    from baibai_jobs.integration.jpx_handoff import export_handoff as export_jpx
    from baibai_jobs.integration.tradingview_handoff import export_handoff as export_tv

    tv_source = store(tmp_path / "tv-source.sqlite", 1)
    retain_result(tv_source, _sdk(payload(["TSE:1000"])))
    remote, master, calendar = native_inputs(tv_source, tmp_path, day=DAY)
    tv = tmp_path / "tv-packet"
    tv.mkdir()
    handoff = export_tv(
        tv_source,
        tv / "receipts.sqlite",
        day=DAY,
        since=NOW,
        master_input=master,
        calendar_input=calendar,
    )
    (tv / "result.json").write_text(
        json.dumps(
            {
                "source": "tradingview",
                "started_at": NOW.isoformat(),
                "asof": DAY.isoformat(),
                **handoff.to_json(),
            }
        )
    )
    save_handoff(remote, "tradingview-ready", tv, market=True)

    jpx = tmp_path / "jpx-packet"
    jpx.mkdir()
    jpx_source = tmp_path / "jpx-source.sqlite"
    earnings(jpx_source, STAMP, "2026-10-20", "130A")
    bundles = export_jpx(
        jpx_source, jpx / "receipts.sqlite", since=STAMP, collection="earnings_calendar"
    )
    (jpx / "result.json").write_text(
        json.dumps(
            {
                "source": "jpx-earnings",
                "started_at": STAMP.isoformat(),
                "asof": JPX_DAY.isoformat(),
                "bundle_ids": bundles,
            }
        )
    )
    save_handoff(remote, "manual-jpx", jpx, market=True)
    work = tmp_path / "integration"
    work.mkdir()
    (work / "plan.json").write_text(
        json.dumps(
            {
                "handoffs": available_handoffs(remote, market=True),
                "edinet_input": empty_edinet_input(remote, tmp_path).model_dump(mode="json"),
            }
        )
    )

    # Current source-root structure is global; packed evidence is requested only
    # for the actual dated evidence from this verified TradingView packet.
    corrupt_key = None
    if fault in {"older-pack", "required-pack", "required-transport"}:
        from tests.engine.data_collection.providers.jquants.test_calendar_native import attempt

        from baibai_engine.data_collection.providers.jquants.calendar_native import (
            PREFIX,
            CalendarL1,
        )

        fixed = CalendarL1(remote, tmp_path / "mutable-calendar")
        raw_date = DAY
        if fault == "older-pack":
            raw_date -= timedelta(days=366)
            fixed.commit([attempt("unrelated-calendar", raw_date)])
        root = fixed.fixed().value()
        adopted = root["days"][raw_date.isoformat()]["adopted"]
        digest = root["receipts"][adopted]["pack"]
        corrupt_key = PREFIX + "packs/" + digest + ".json.gz"
        if fault == "required-transport":
            get = remote.get_bytes

            def fail_required_pack(key):
                if key == corrupt_key:
                    raise R2Error("transport unavailable")
                return get(key)

            monkeypatch.setattr(remote, "get_bytes", fail_required_pack)
        else:
            remote.values[corrupt_key].body = b"not the expected original"
    else:
        _corrupt_tradingview_heads(remote, CALENDAR, MASTER, empty, malformed, fault)

    market = tmp_path / "market.sqlite"
    with closing(open_connection(market)):
        pass
    if fault in {"shape", "clock"}:
        before = market.read_bytes()
        reason = "must be an object" if fault == "shape" else "Invalid isoformat string"
        with pytest.raises(ValueError, match=reason):
            apply_market(remote, work, market=market, asof=DAY)
        assert market.read_bytes() == before
        return

    remote.get_keys.clear()
    remote.head_keys.clear()
    if fault == "required-transport":
        with pytest.raises(R2Error, match="transport unavailable"):
            apply_market(remote, work, market=market, asof=DAY)
        with closing(open_connection(market)) as conn:
            assert not conn.execute("SELECT 1 FROM tradingview_source_receipts").fetchone()
        return

    result = apply_market(remote, work, market=market, asof=DAY)
    good, tradingview = result["handoffs"]
    assert good["applied"] is True
    assert good["failed"] is False
    if fault == "older-pack":
        assert corrupt_key not in remote.get_keys
        assert corrupt_key not in remote.head_keys
        assert tradingview["applied"] is True
        assert tradingview["failed"] is False
    else:
        assert tradingview["result"] == {
            "reason": (
                "LakePublishError"
                if fault == "required-pack"
                else "tradingview_native_context_unavailable"
            )
        }
        assert tradingview["applied"] is False
        assert tradingview["failed"] is tradingview["recovery"] is True
    with closing(open_connection(market)) as conn:
        assert conn.execute("SELECT ticker FROM jpx_earnings_calendar").fetchall() == [("130A",)]
        assert bool(conn.execute("SELECT 1 FROM tradingview_source_receipts").fetchone()) is (
            fault == "older-pack"
        )
    assert "coordination/market-sources/tradingview-ready/integrated.json" not in remote.values


def _corrupt_tradingview_heads(remote, calendar, master, empty, malformed, fault):
    # Change mutable head structure independently of the packet's fixed histories.
    for owner, key, fields in (
        ("calendar", calendar, ("days", "ranges", "receipts")),
        ("master", master, ("scopes", "receipts")),
    ):
        head = json.loads(remote.values[key].body)
        if owner == empty:
            head.update({field: {} for field in fields})
        elif owner == malformed:
            if fault == "shape":
                head[fields[0]] = []
            else:
                next(iter(head[fields[0]].values()))["confirmed_at"] = "bad-clock"
        else:
            continue
        remote.store_object(key, json.dumps(head).encode())


@pytest.mark.parametrize("field", ["receipt_sha256", "receipt_bytes", "receipt_key"])
def test_incomplete_handoff_manifest_is_local_and_other_source_still_integrates(
    tmp_path, monkeypatch, field
):
    store, work, market, economic = _mixed_handoffs(tmp_path, monkeypatch)
    key = "coordination/market-sources/jpx-regulations-fault/handoff.json"
    manifest = json.loads(store.values[key].body)
    del manifest[field]
    store.store_object(key, json.dumps(manifest).encode())
    entries = available_handoffs(store)
    entries.sort(key=lambda entry: not entry["market"])
    plan = json.loads((work / "plan.json").read_text())
    plan["handoffs"] = entries
    (work / "plan.json").write_text(json.dumps(plan))

    report = apply(store, work, market=market, economic=economic, asof=date(2026, 10, 24))
    bad, good = report["handoffs"]
    assert bad["result"] == {"reason": "ValueError"}
    assert bad["applied"] is False
    assert bad["failed"] is bad["recovery"] is True
    assert good["applied"] is True
    assert good["failed"] is False
    with closing(initialize_database(economic)) as conn:
        assert next(
            read_snapshots(conn, date(2026, 10, 24), date(2026, 10, 24), source_id="fred-payems")
        ).records == ({"value": "independent receipt"},)


@pytest.mark.parametrize(
    ("source", "metadata"),
    [
        ("jpx-regulations", {}),
        ("jpx-earnings", {"bundle_ids": "not-an-id-list"}),
        ("tradingview", {"symbols": [], "receipt_ids": []}),
        (
            "tradingview",
            {
                "snapshot_date": "2026-10-24",
                "master_asof": "2026-10-23",
                "symbols": [],
                "receipt_ids": [None],
            },
        ),
    ],
)
def test_invalid_market_metadata_is_local_and_other_source_still_integrates(
    tmp_path, monkeypatch, source, metadata
):
    store, work, market, economic = _mixed_handoffs(
        tmp_path,
        monkeypatch,
        market_report={
            "source": source,
            "asof": "2026-10-24",
            "started_at": FIRST_AT.isoformat(),
            **metadata,
        },
    )
    report = apply(store, work, market=market, economic=economic, asof=date(2026, 10, 24))
    bad, good = report["handoffs"]
    assert bad["result"] == {"reason": "ValueError"}
    assert bad["applied"] is False
    assert bad["failed"] is bad["recovery"] is True
    assert good["applied"] is True
    assert good["failed"] is False
    with closing(initialize_database(economic)) as conn:
        assert next(
            read_snapshots(conn, date(2026, 10, 24), date(2026, 10, 24), source_id="fred-payems")
        ).records == ({"value": "independent receipt"},)
