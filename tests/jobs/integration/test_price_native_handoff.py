"""Old mixed stages verify migrated price bytes without rewriting native-owned prices."""

import gzip
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.edinet_native import empty_edinet_input
from tests.helpers.jquants_receipts import body
from tests.helpers.lake_release import market_store
from tests.helpers.r2_store import MemoryR2Store

import baibai_jobs.integration.source_handoff as handoff
import baibai_jobs.integration.sources as integration
from baibai_engine.data_collection.providers.jquants.contract import JQUANTS_SOURCE_PAGES_DDL
from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.financial_native import FinancialAttempt
from baibai_engine.data_collection.providers.jquants.master_native import MasterAttempt, MasterL1
from baibai_engine.data_collection.providers.jquants.price_native import (
    HEAD_KEY,
    PriceAttempt,
    PriceL1,
)
from baibai_engine.storage.json import write_json_atomic
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.acquisition.receipts import available_handoffs
from baibai_jobs.integration.price_native import model_input

DAY = date(2026, 10, 30)
AT = datetime(2026, 10, 30, 8, tzinfo=UTC)


@pytest.fixture(autouse=True)
def native_margin_replay_proofs(monkeypatch):
    replay = handoff._replay

    def with_margin_proofs(*args, **kwargs):
        for name in (
            "margin_alerts_fingerprints",
            "short_sale_reports_fingerprints",
            "margin_interest_fingerprints",
        ):
            kwargs.setdefault(name, {})
        return replay(*args, **kwargs)

    monkeypatch.setattr(handoff, "_replay", with_margin_proofs)


@pytest.fixture(autouse=True)
def native_flags(monkeypatch):
    monkeypatch.setenv("BAIBAI_PRICE_NATIVE", "true")
    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", "false")
    monkeypatch.setenv("BAIBAI_FINANCIAL_NATIVE", "false")
    monkeypatch.setenv("BAIBAI_CALENDAR_NATIVE", "false")


def price_attempt(state="valid"):
    rows = [{"Code": "13010", "Date": DAY.isoformat(), "C": 100}]
    if state == "empty":
        rows = []
    elif state == "projection-partial":
        rows.append({"Date": DAY.isoformat(), "C": 999})
    pages = (
        JQuantsPage(
            "daily_bars",
            {"date": DAY.isoformat()},
            AT,
            body(rows, cursor="next" if state == "incomplete" else None),
        ),
    )
    # Migration retains old request clocks and records no invented attempt time.
    return PriceAttempt("migrated-price", DAY, None, pages)


def write_packet(directory, old, *, source="jquants-market", calendar=True):
    directory.mkdir(parents=True)
    requests = {old.request_id: old.pages}
    if calendar:
        requests["calendar"] = (
            JQuantsPage(
                "market_calendar",
                {"from": DAY.isoformat(), "to": DAY.isoformat()},
                AT,
                body([{"Date": DAY.isoformat(), "HolDiv": "1"}]),
            ),
        )
    with sqlite3.connect(directory / "receipts.sqlite") as conn:
        conn.executescript(JQUANTS_SOURCE_PAGES_DDL)
        for identity, pages in requests.items():
            for index, page in enumerate(pages):
                conn.execute(
                    "INSERT INTO jquants_source_pages VALUES (?,?,?,?,?,?,?)",
                    (
                        identity,
                        index,
                        page.source,
                        json.dumps(dict(page.query)),
                        page.received_at.isoformat(),
                        DAY.isoformat(),
                        gzip.compress(page.body, mtime=0),
                    ),
                )
    write_json_atomic(
        directory / "result.json",
        {
            "source": source,
            "asof": DAY.isoformat(),
            "started_at": AT.isoformat(),
            "request_ids": list(requests),
            "request_ids_complete": True,
        },
    )
    return directory


def context(tmp_path, state="valid", *, migrated=True):
    store = MemoryR2Store()
    from tests.helpers.native_margin_sources import initialize_margin_sources

    initialize_margin_sources(store, tmp_path / "empty-margin-baseline")
    store.close = lambda: None
    owner = PriceL1(store, tmp_path / "native")
    owner.initialize()
    old = price_attempt(state)
    if migrated:
        owner.commit([old])
    else:
        owner.commit([PriceAttempt("other-price", DAY, None, price_attempt().pages)])
    market = market_store(tmp_path / "market.sqlite")
    model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    packet = write_packet(tmp_path / "packet", old)
    fingerprints = {
        identity: receipt["fingerprint"]
        for identity, receipt in owner.fixed().value()["receipts"].items()
    }
    return store, owner, market, packet, fingerprints


def rows(database, table):
    with sqlite3.connect(database) as conn:
        return conn.execute(f"SELECT * FROM {table}").fetchall()


@pytest.mark.parametrize("state", ["valid", "empty", "incomplete", "projection-partial"])
def test_migrated_price_does_not_block_calendar_or_rewrite_native_projection(tmp_path, state):
    store, owner, market, packet, fingerprints = context(tmp_path, state)
    before = rows(market, "jquants_daily_bars")
    coverage = rows(market, "source_coverage")
    head = store.values[HEAD_KEY].body
    replay = handoff._replay("jquants-market", market, packet, price_fingerprints=fingerprints)
    assert replay.complete
    assert replay.report["migrated_price_requests"] == ["migrated-price"]
    assert replay.report["replayed_requests"] == ["calendar"]
    assert replay.report["raw_pages_added"] == 1
    assert rows(market, "jquants_daily_bars") == before
    assert set(coverage) <= set(rows(market, "source_coverage"))
    assert rows(market, "jquants_market_calendar") == [(DAY.isoformat(), "1")]
    with sqlite3.connect(market) as conn:
        assert conn.execute("SELECT DISTINCT source FROM jquants_source_pages").fetchall() == [
            ("market_calendar",)
        ]
    assert store.values[HEAD_KEY].body == head
    # The next normal generation keeps the same native values and the independent calendar.
    model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    assert rows(market, "jquants_daily_bars") == before
    assert rows(market, "jquants_market_calendar") == [(DAY.isoformat(), "1")]


@pytest.mark.parametrize(
    "fault",
    ["unknown-id", "body", "clock", "scope", "filtered", "missing-page", "mixed-source", "gzip"],
)
def test_unmigrated_or_changed_price_is_rejected_before_any_raw_import(tmp_path, fault):
    store, _owner, market, packet, fingerprints = context(tmp_path)
    with sqlite3.connect(packet / "receipts.sqlite") as conn:
        if fault == "unknown-id":
            fingerprints = {}
        elif fault == "body":
            conn.execute(
                "UPDATE jquants_source_pages SET body_gzip=? WHERE request_id='migrated-price'",
                (gzip.compress(body([{"Code": "13010", "Date": DAY.isoformat(), "C": 888}])),),
            )
        elif fault == "clock":
            conn.execute(
                "UPDATE jquants_source_pages SET received_at_utc=? WHERE request_id='migrated-price'",
                ((AT + timedelta(seconds=1)).isoformat(),),
            )
        elif fault == "scope":
            conn.execute(
                "UPDATE jquants_source_pages SET query_json=? WHERE request_id='migrated-price'",
                (json.dumps({"date": (DAY + timedelta(days=1)).isoformat()}),),
            )
        elif fault == "filtered":
            conn.execute(
                "UPDATE jquants_source_pages SET query_json=? WHERE request_id='migrated-price'",
                (json.dumps({"date": DAY.isoformat(), "code": "13010"}),),
            )
        elif fault == "missing-page":
            conn.execute(
                "UPDATE jquants_source_pages SET page_index=1 WHERE request_id='migrated-price'"
            )
        elif fault == "mixed-source":
            conn.execute(
                "INSERT INTO jquants_source_pages SELECT request_id,1,'master',query_json,"
                "received_at_utc,received_on,body_gzip FROM jquants_source_pages "
                "WHERE request_id='migrated-price'"
            )
        else:
            conn.execute(
                "UPDATE jquants_source_pages SET body_gzip=x'00' WHERE request_id='migrated-price'"
            )
    before, head = market.read_bytes(), store.values[HEAD_KEY].body
    original = (packet / "receipts.sqlite").read_bytes()
    with pytest.raises(ValueError, match="legacy daily_bars"):
        handoff._replay("jquants-market", market, packet, price_fingerprints=fingerprints)
    assert market.read_bytes() == before
    assert store.values[HEAD_KEY].body == head
    assert (packet / "receipts.sqlite").read_bytes() == original


def test_native_replay_requires_explicit_price_fingerprints(tmp_path):
    _store, _owner, market, packet, _fingerprints = context(tmp_path)
    before = market.read_bytes()
    with pytest.raises(
        ValueError, match="native price replay requires retained cutover fingerprints"
    ):
        handoff._replay("jquants-market", market, packet)
    assert market.read_bytes() == before


@pytest.mark.parametrize("command", ["replay-copy", "resume", "apply"])
@pytest.mark.parametrize("migrated", [True, False])
@pytest.mark.parametrize("master_native", [True, False])
def test_real_entrypoints_check_price_migration_before_replaying_mixed_stages(
    tmp_path, monkeypatch, command, migrated, master_native
):
    store, _owner, market, packet, _fingerprints = context(
        tmp_path, "projection-partial", migrated=migrated
    )
    if master_native:
        monkeypatch.setenv("BAIBAI_MASTER_NATIVE", "true")
        master = MasterL1(store, tmp_path / "master-native")
        master.initialize()
        master.commit(
            [
                MasterAttempt(
                    "other-master",
                    DAY,
                    None,
                    (JQuantsPage("master", {"date": DAY.isoformat()}, AT, body([])),),
                )
            ]
        )
    monkeypatch.setattr(handoff, "Boto3R2Store", lambda **_: store)
    before = rows(market, "jquants_daily_bars")
    head = store.values[HEAD_KEY].body
    directory = packet
    if command != "replay-copy":
        save_handoff(store, "jquants-market-synthetic", packet, market=True)
        directory = tmp_path / "resume-or-plan"
        directory.mkdir()
        write_json_atomic(directory / "result.json", {"created_at": AT.isoformat()})
    if command == "apply":
        write_json_atomic(
            directory / "plan.json",
            {
                "edinet_input": empty_edinet_input(store, tmp_path).model_dump(mode="json"),
                "financial_native": False,
                "price_native": True,
                "master_native": master_native,
                "handoffs": available_handoffs(store, source="jquants-market"),
                "external": [],
            },
        )
        outcome = integration.apply(store, directory, market=market, asof=DAY)["handoffs"]
        assert len(outcome) == 1
        assert outcome[0]["failed"] is not migrated
        assert outcome[0]["applied"] is migrated
        assert outcome[0]["recovery"] is not migrated
    else:
        status = handoff.main(
            [
                command,
                "--source",
                "jquants-market",
                "--directory",
                str(directory),
                "--database",
                str(market),
                "--asof",
                DAY.isoformat(),
            ]
        )
        assert status == int(not migrated)
    assert rows(market, "jquants_daily_bars") == before
    assert rows(market, "jquants_market_calendar") == ([(DAY.isoformat(), "1")] if migrated else [])
    with sqlite3.connect(market) as conn:
        assert conn.execute("SELECT DISTINCT source FROM jquants_source_pages").fetchall() == (
            [("market_calendar",)] if migrated else []
        )
    assert store.values[HEAD_KEY].body == head


@pytest.mark.parametrize("command", ["replay-copy", "resume", "apply"])
def test_financial_native_rejects_the_explicit_legacy_replay_cli(tmp_path, monkeypatch, command):
    monkeypatch.setenv("BAIBAI_FINANCIAL_NATIVE", "true")
    page = JQuantsPage(
        "financial_summaries",
        {"date": DAY.isoformat()},
        AT,
        body([{"Code": "13010", "DiscDate": DAY.isoformat(), "DiscNo": "1", "Sales": 180}]),
    )
    old = FinancialAttempt("legacy-financial", DAY, None, (page,))
    packet = write_packet(tmp_path / "financial", old, source="jquants-financial", calendar=False)
    market = market_store(tmp_path / "market.sqlite")
    before = market.read_bytes()
    original = (packet / "receipts.sqlite").read_bytes()
    store = MemoryR2Store()
    from tests.helpers.native_margin_sources import initialize_margin_sources

    initialize_margin_sources(store, tmp_path / "empty-margin-baseline")
    store.close = lambda: None
    monkeypatch.setattr(handoff, "Boto3R2Store", lambda **_: store)
    directory = packet
    if command != "replay-copy":
        save_handoff(store, "jquants-financial-synthetic", packet, market=True)
        directory = tmp_path / "resume-or-plan"
        directory.mkdir()
        write_json_atomic(directory / "result.json", {"created_at": AT.isoformat()})
    if command == "apply":
        write_json_atomic(
            directory / "plan.json",
            {
                "edinet_input": empty_edinet_input(store, tmp_path).model_dump(mode="json"),
                "financial_native": True,
                "master_native": False,
                "price_native": True,
                "handoffs": available_handoffs(store, source="jquants-financial"),
            },
        )
        outcome = integration.apply(store, directory, market=market, asof=DAY)["handoffs"]
        assert len(outcome) == 1
        assert outcome[0]["failed"] is True
        assert outcome[0]["applied"] is False
        assert outcome[0]["recovery"] is True
    else:
        assert (
            handoff.main(
                [
                    command,
                    "--source",
                    "jquants-financial",
                    "--directory",
                    str(directory),
                    "--database",
                    str(market),
                    "--asof",
                    DAY.isoformat(),
                ]
            )
            == 1
        )
    assert market.read_bytes() == before
    assert (packet / "receipts.sqlite").read_bytes() == original


def test_apply_without_market_stages_does_not_read_native_price_head(tmp_path):
    store = MemoryR2Store()
    from tests.helpers.native_margin_sources import initialize_margin_sources

    initialize_margin_sources(store, tmp_path / "empty-margin-baseline")
    market = market_store(tmp_path / "market.sqlite")
    write_json_atomic(
        tmp_path / "plan.json",
        {
            "edinet_input": empty_edinet_input(store, tmp_path).model_dump(mode="json"),
            "financial_native": False,
            "master_native": False,
            "price_native": True,
            "handoffs": [],
        },
    )
    store.get_keys.clear()
    store.head_keys.clear()
    assert integration.apply(store, tmp_path, market=market, asof=DAY)["handoffs"] == []
    assert HEAD_KEY not in store.get_keys
    assert HEAD_KEY not in store.head_keys


@pytest.mark.parametrize("state", ["valid", "empty", "incomplete", "projection-partial"])
def test_migrated_price_only_stage_is_a_noop(tmp_path, state):
    store, _owner, market, _packet, fingerprints = context(tmp_path, state)
    packet = write_packet(tmp_path / "price-only", price_attempt(state), calendar=False)
    before = market.read_bytes()
    head = store.values[HEAD_KEY].body
    result = handoff._replay("jquants-market", market, packet, price_fingerprints=fingerprints)
    assert result.complete
    assert result.report["migrated_price_requests"] == ["migrated-price"]
    assert result.report["raw_pages_added"] == 0
    assert result.report["replayed_requests"] == []
    assert market.read_bytes() == before
    assert store.values[HEAD_KEY].body == head


def test_mixed_master_and_price_are_both_skipped_but_calendar_is_replayed(tmp_path, monkeypatch):
    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", "true")
    store, _owner, market, packet, fingerprints = context(tmp_path, "projection-partial")
    master = MasterL1(store, tmp_path / "master")
    master.initialize()
    page = JQuantsPage("master", {"date": DAY.isoformat()}, AT, body([]))
    master.commit([MasterAttempt("migrated-master", DAY, None, (page,))])
    with sqlite3.connect(packet / "receipts.sqlite") as conn:
        conn.execute(
            "INSERT INTO jquants_source_pages VALUES (?,?,?,?,?,?,?)",
            (
                "migrated-master",
                0,
                "master",
                json.dumps(dict(page.query)),
                AT.isoformat(),
                DAY.isoformat(),
                gzip.compress(page.body, mtime=0),
            ),
        )
    report = json.loads((packet / "result.json").read_bytes())
    report["request_ids"].append("migrated-master")
    write_json_atomic(packet / "result.json", report)
    result = handoff._replay(
        "jquants-market",
        market,
        packet,
        price_fingerprints=fingerprints,
        master_fingerprints={
            identity: receipt["fingerprint"]
            for identity, receipt in master.fixed().value()["receipts"].items()
        },
    )
    assert result.complete
    assert result.report["migrated_master_requests"] == ["migrated-master"]
    assert result.report["migrated_price_requests"] == ["migrated-price"]
    assert result.report["replayed_requests"] == ["calendar"]
    with sqlite3.connect(market) as conn:
        assert conn.execute("SELECT DISTINCT source FROM jquants_source_pages").fetchall() == [
            ("market_calendar",)
        ]


@pytest.mark.parametrize("command", ["replay-copy", "resume", "apply"])
def test_all_native_fingerprints_survive_combined_cutover_entrypoints(
    tmp_path, monkeypatch, command
):
    from baibai_engine.data_collection.providers.jquants.calendar_native import (
        HEAD_KEY as CALENDAR_HEAD,
    )
    from baibai_engine.data_collection.providers.jquants.calendar_native import (
        CalendarAttempt,
        CalendarL1,
    )
    from baibai_engine.data_collection.providers.jquants.master_native import (
        HEAD_KEY as MASTER_HEAD,
    )
    from baibai_jobs.integration.calendar_native import model_input as calendar_input

    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", "true")
    monkeypatch.setenv("BAIBAI_CALENDAR_NATIVE", "true")
    store, _owner, market, packet, _fingerprints = context(tmp_path, "projection-partial")
    calendar = CalendarL1(store, tmp_path / "calendar-native")
    calendar.initialize()
    calendar_page = JQuantsPage(
        "market_calendar",
        {"from": DAY.isoformat(), "to": DAY.isoformat()},
        AT,
        body([{"Date": DAY.isoformat(), "HolDiv": "1"}]),
    )
    calendar.commit([CalendarAttempt("calendar", DAY, DAY, None, (calendar_page,))])
    calendar_input(store, calendar.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    master = MasterL1(store, tmp_path / "master-native")
    master.initialize()
    master_page = JQuantsPage("master", {"date": DAY.isoformat()}, AT, body([]))
    master.commit([MasterAttempt("migrated-master", DAY, None, (master_page,))])
    margin_page = JQuantsPage("margin_alerts", {"date": DAY.isoformat()}, AT, body([]))
    from baibai_engine.data_collection.providers.jquants.margin_alerts_native import (
        MarginAlertsAttempt,
        MarginAlertsL1,
    )

    MarginAlertsL1(store, tmp_path / "alerts-native").commit(
        [MarginAlertsAttempt("margin", DAY, None, (margin_page,))]
    )
    report = json.loads((packet / "result.json").read_bytes())
    with sqlite3.connect(packet / "receipts.sqlite") as conn:
        for identity, page in (("migrated-master", master_page), ("margin", margin_page)):
            conn.execute(
                "INSERT INTO jquants_source_pages VALUES (?,?,?,?,?,?,?)",
                (
                    identity,
                    0,
                    page.source,
                    json.dumps(dict(page.query)),
                    AT.isoformat(),
                    DAY.isoformat(),
                    gzip.compress(page.body, mtime=0),
                ),
            )
            report["request_ids"].append(identity)
    write_json_atomic(packet / "result.json", report)
    before = rows(market, "jquants_daily_bars")
    heads = {key: store.values[key].body for key in (HEAD_KEY, MASTER_HEAD, CALENDAR_HEAD)}
    monkeypatch.setattr(handoff, "Boto3R2Store", lambda **_: store)
    directory = packet
    if command != "replay-copy":
        save_handoff(store, "jquants-market-combined", packet, market=True)
        directory = tmp_path / "resume-or-plan"
        directory.mkdir()
        write_json_atomic(directory / "result.json", {"created_at": AT.isoformat()})
    if command == "apply":
        write_json_atomic(
            directory / "plan.json",
            {
                "financial_native": False,
                "price_native": True,
                "master_native": True,
                "calendar_native": True,
                "edinet_input": empty_edinet_input(store, tmp_path).model_dump(mode="json"),
                "handoffs": available_handoffs(store, source="jquants-market"),
            },
        )
        outcome = integration.apply(store, directory, market=market, asof=DAY)["handoffs"]
        assert len(outcome) == 1
        assert outcome[0]["applied"] is True
        assert outcome[0]["failed"] is False
        assert outcome[0]["result"]["migrated_master_requests"] == ["migrated-master"]
        assert outcome[0]["result"]["migrated_price_requests"] == ["migrated-price"]
        assert outcome[0]["result"]["migrated_calendar_requests"] == ["calendar"]
        assert outcome[0]["result"]["migrated_margin_alerts_requests"] == ["margin"]
        assert outcome[0]["result"]["replayed_requests"] == []
    else:
        assert (
            handoff.main(
                [
                    command,
                    "--source",
                    "jquants-market",
                    "--directory",
                    str(directory),
                    "--database",
                    str(market),
                    "--asof",
                    DAY.isoformat(),
                ]
            )
            == 0
        )
    assert rows(market, "jquants_daily_bars") == before
    assert rows(market, "jquants_market_calendar") == [(DAY.isoformat(), "1")]
    with sqlite3.connect(market) as conn:
        assert conn.execute("SELECT DISTINCT source FROM jquants_source_pages").fetchall() == []
    assert {key: store.values[key].body for key in heads} == heads
