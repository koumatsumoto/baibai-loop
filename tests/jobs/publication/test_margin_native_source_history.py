"""Margin-source cutovers keep the remaining mixed-original history floor."""

import gzip
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store
from tests.helpers.margin_alerts_native import attempt as alert_attempt
from tests.helpers.r2_store import MemoryR2Store

import baibai_jobs.publication.snapshot_publication as publication
from baibai_engine.data_collection.providers.jquants.margin_alerts_native import MarginAlertsL1
from baibai_engine.machine_analysis.observations.jquants_writer import store_jquants_margin_alerts
from baibai_jobs.publication.datasets import LAKE_DATASETS

DAY = date(2026, 10, 1)
AT = datetime(2026, 10, 1, tzinfo=UTC)
POINTER = "lake/pointers/market/current.json"


@pytest.fixture(params=["margin_alerts"])
def native_source(request):
    return request.param


@pytest.fixture
def native_margin_release(tmp_path, monkeypatch, native_source):
    names = ("jquants.source_pages", "jquants.margin_alerts", "jquants.short_sale_reports")
    narrow_release_policy(monkeypatch, datasets=names, carries_history=None)
    monkeypatch.setattr(publication, "LAKE_DATASETS", {name: LAKE_DATASETS[name] for name in names})
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)
    monkeypatch.setenv("BAIBAI_MARGIN_ALERTS_NATIVE", "false")

    def create(remaining=(), *, migrated=True):
        store = MemoryR2Store()
        owner = MarginAlertsL1(store, tmp_path / "native")
        owner.initialize()
        received = replace(alert_attempt("migrated", day=DAY, at=AT), attempted_at=None)
        if migrated:
            owner.commit([received])
        market = market_store(tmp_path / "market.sqlite")
        with sqlite3.connect(market) as connection:
            for index, page in enumerate(received.pages):
                connection.execute(
                    "INSERT INTO jquants_source_pages VALUES (?,?,?,?,?,?,?)",
                    (
                        received.request_id,
                        index,
                        page.source,
                        json.dumps(dict(page.query)),
                        page.received_at.isoformat(),
                        page.received_at.date().isoformat(),
                        gzip.compress(page.body, mtime=0),
                    ),
                )
            for index, (source, received_on) in enumerate(remaining):
                connection.execute(
                    "INSERT INTO jquants_source_pages VALUES (?,?,?,?,?,?,?)",
                    (
                        f"remaining-{index}",
                        0,
                        source,
                        "{}",
                        received_on + "T00:00:00+00:00",
                        received_on,
                        gzip.compress(b'{"data":[]}', mtime=0),
                    ),
                )
        store_jquants_margin_alerts(
            market,
            json.loads(received.pages[0].body)["data"],
            requested_start=DAY,
            requested_end=DAY,
            fetched_at_utc=AT.isoformat(),
        )
        mirror = tmp_path / "mirror"
        with publication.prepare_market_lake(
            sqlite_path=market, mirror_root=mirror, store=store, release_id="old-margin"
        ) as prepared:
            publication.publish_prepared_market_lake(prepared, store=store)
        originals = {key: item.body for key, item in store.values.items() if key != POINTER}
        return store, market, mirror, originals

    return create


def enable_native(monkeypatch, native_source):
    monkeypatch.setenv(f"BAIBAI_{native_source.upper()}_NATIVE", "true")


def test_margin_cutover_uses_remaining_floor_and_preserves_originals(
    native_margin_release, native_source, monkeypatch
):
    store, market, mirror, originals = native_margin_release([("margin_interest", "2026-10-02")])
    enable_native(monkeypatch, native_source)
    with publication.prepare_market_lake(
        sqlite_path=market, mirror_root=mirror, store=store, as_of=DAY, release_id="native-margin"
    ) as prepared:
        assert prepared.export.datasets["jquants.source_pages"].manifest.coverage_start == date(
            2026, 10, 2
        )
        publication.publish_prepared_market_lake(prepared, store=store)
    assert all(store.values[key].body == body for key, body in originals.items())
    with sqlite3.connect(market) as connection:
        assert connection.execute("SELECT source FROM jquants_source_pages").fetchall() == [
            ("margin_interest",)
        ]


@pytest.mark.parametrize("lost_source", ["margin_interest", "short_sale_reports", "master"])
@pytest.mark.parametrize("lose_all", [False, True])
def test_margin_cutover_does_not_exempt_remaining_originals(
    native_margin_release, native_source, monkeypatch, lost_source, lose_all
):
    rows = [(lost_source, "2026-10-02")]
    if not lose_all:
        rows.append(("financial_summaries", "2026-10-03"))
    store, market, mirror, _ = native_margin_release(rows)
    pointer = store.values[POINTER].body
    with sqlite3.connect(market) as connection:
        connection.execute("DELETE FROM jquants_source_pages WHERE source=?", (lost_source,))
    enable_native(monkeypatch, native_source)
    message = "historical dataset" if lose_all else "coverage starts 2026-10-03"
    with (
        pytest.raises(ValueError, match=message),
        publication.prepare_market_lake(
            sqlite_path=market, mirror_root=mirror, store=store, as_of=DAY, release_id="lost"
        ),
    ):
        pytest.fail("remaining originals must retain their old history floor")
    assert store.values[POINTER].body == pointer


def test_margin_native_only_pages_may_leave_optional_dataset(
    native_margin_release, native_source, monkeypatch
):
    store, market, mirror, originals = native_margin_release()
    enable_native(monkeypatch, native_source)
    with publication.prepare_market_lake(
        sqlite_path=market, mirror_root=mirror, store=store, as_of=DAY, release_id="only-native"
    ) as prepared:
        assert "jquants.source_pages" not in prepared.release.datasets
        publication.publish_prepared_market_lake(prepared, store=store)
    assert all(store.values[key].body == body for key, body in originals.items())


def test_disabled_margin_flag_keeps_legacy_floor(native_margin_release, native_source):
    store, market, mirror, _ = native_margin_release([("margin_interest", "2026-10-02")])
    with sqlite3.connect(market) as connection:
        connection.execute("DELETE FROM jquants_source_pages WHERE source=?", (native_source,))
    with (
        pytest.raises(ValueError, match="coverage starts 2026-10-02"),
        publication.prepare_market_lake(
            sqlite_path=market, mirror_root=mirror, store=store, as_of=DAY, release_id="legacy-loss"
        ),
    ):
        pytest.fail("a disabled native flag cannot exempt original history")


def test_unmigrated_margin_receipt_still_blocks_before_history_adjustment(
    native_margin_release, native_source, monkeypatch
):
    store, market, mirror, _ = native_margin_release(
        [("margin_interest", "2026-10-02")], migrated=False
    )
    pointer = store.values[POINTER].body
    enable_native(monkeypatch, native_source)
    with (
        pytest.raises(ValueError, match="absent from or differs from the native cutover"),
        publication.prepare_market_lake(
            sqlite_path=market, mirror_root=mirror, store=store, as_of=DAY, release_id="unmigrated"
        ),
    ):
        pytest.fail("an enabled flag does not prove the raw receipt was migrated")
    assert store.values[POINTER].body == pointer
    with sqlite3.connect(market) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM jquants_source_pages WHERE source=?", (native_source,)
        ).fetchone() == (1,)
