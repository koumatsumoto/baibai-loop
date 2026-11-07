"""Native cutover changes mixed-page membership without losing legacy history."""

import gzip
import sqlite3
from datetime import date

import pytest
from tests.engine.data_collection.providers.jquants.test_calendar_native import (
    attempt as calendar_attempt,
)
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store
from tests.helpers.r2_store import MemoryR2Store
from tests.jobs.integration.test_master_native_models import attempt as master_attempt
from tests.jobs.integration.test_price_native_models import DAY
from tests.jobs.integration.test_price_native_models import attempt as price_attempt

import baibai_jobs.publication.snapshot_publication as publication
from baibai_engine.data_collection.providers.jquants.calendar_native import CalendarL1
from baibai_engine.data_collection.providers.jquants.master_native import MasterL1
from baibai_engine.data_collection.providers.jquants.price_native import PriceL1
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.publication.datasets import LAKE_DATASETS

POINTER = "lake/pointers/market/current.json"


@pytest.fixture
def mixed_release(tmp_path, monkeypatch):
    names = ("jquants.source_pages", "jquants.short_sale_reports")
    narrow_release_policy(monkeypatch, datasets=names, carries_history=None)
    monkeypatch.setattr(publication, "LAKE_DATASETS", {n: LAKE_DATASETS[n] for n in names})
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)

    def create(rows):
        store = MemoryR2Store()
        for owner_type, label, received in (
            (PriceL1, "price", price_attempt("price", day=DAY)),
            (MasterL1, "master", master_attempt("master", day=DAY)),
            (CalendarL1, "calendar", calendar_attempt("calendar", start=DAY)),
        ):
            owner = owner_type(store, tmp_path / label)
            owner.initialize()
            owner.commit([received])
        market = market_store(tmp_path / "market.sqlite")
        with sqlite3.connect(market) as connection:
            for index, (source, received) in enumerate(rows):
                connection.execute(
                    "INSERT INTO jquants_source_pages VALUES (?,?,?,?,?,?,?)",
                    (
                        f"legacy-{index}",
                        0,
                        source,
                        "{}",
                        received + "T00:00:00+00:00",
                        received,
                        gzip.compress(b'{"data":[]}'),
                    ),
                )
        from tests.helpers.native_margin_sources import seed_short_reports

        seed_short_reports(store, market, tmp_path / "short-baseline")
        mirror = tmp_path / "mirror"
        with publication.prepare_market_lake(
            sqlite_path=market, mirror_root=mirror, store=store, release_id="old-mixed"
        ) as prepared:
            publication.publish_prepared_market_lake(prepared, store=store)
            old_pages = prepared.export.datasets["jquants.source_pages"].manifest
        return store, market, mirror, old_pages

    return create


def enable_native(monkeypatch, source):
    flags = {
        "daily_bars": ("PRICE",),
        "master": ("MASTER",),
        "market_calendar": ("FINANCIAL", "PRICE", "MASTER", "CALENDAR"),
    }[source]
    for flag in flags:
        monkeypatch.setenv(f"BAIBAI_{flag}_NATIVE", "true")


@pytest.mark.parametrize("source", ["daily_bars", "master", "market_calendar"])
def test_cutover_compares_the_remaining_sources_floor(mixed_release, monkeypatch, source):
    store, market, mirror, old_pages = mixed_release(
        [(source, "2026-01-01"), ("margin_alerts", "2026-02-01")]
    )
    before = {key: item.body for key, item in store.values.items() if key != POINTER}
    enable_native(monkeypatch, source)
    with publication.prepare_market_lake(
        sqlite_path=market, mirror_root=mirror, store=store, as_of=DAY, release_id="native"
    ) as prepared:
        actual = prepared.export.datasets["jquants.source_pages"].manifest
        assert actual.coverage_start == date(2026, 2, 1)
        assert old_pages.coverage_start == date(2026, 1, 1)
        publication.publish_prepared_market_lake(prepared, store=store)
    assert all(store.values[key].body == body for key, body in before.items())
    with sqlite3.connect(market) as connection:
        assert connection.execute("SELECT source FROM jquants_source_pages").fetchall() == [
            ("margin_alerts",)
        ]
    # Once the current release already reflects native ownership, no old-page scan recurs.
    monkeypatch.setattr(
        publication,
        "_remaining_source_pages_start",
        lambda *args: pytest.fail("unchanged legacy floor must not scan old source pages"),
    )
    with publication.prepare_market_lake(
        sqlite_path=market, mirror_root=mirror, store=store, as_of=DAY, release_id="next-native"
    ):
        pass


@pytest.mark.parametrize("remove_all", [False, True])
@pytest.mark.parametrize("unmigrated", ["margin_alerts", "master"])
def test_native_price_still_rejects_lost_unmigrated_history(
    mixed_release, monkeypatch, remove_all, unmigrated
):
    rows = [("daily_bars", "2026-01-01"), (unmigrated, "2026-02-01")]
    if not remove_all:
        rows.append(("weekly_margin", "2026-03-01"))
    store, market, mirror, _ = mixed_release(rows)
    before = store.values[POINTER].body
    with sqlite3.connect(market) as connection:
        connection.execute("DELETE FROM jquants_source_pages WHERE source=?", (unmigrated,))
    enable_native(monkeypatch, "daily_bars")
    message = "historical dataset" if remove_all else "coverage starts 2026-03-01"
    with (
        pytest.raises(ValueError, match=message),
        publication.prepare_market_lake(
            sqlite_path=market, mirror_root=mirror, store=store, as_of=DAY, release_id="lost"
        ),
    ):
        pytest.fail("unmigrated originals cannot disappear")
    assert store.values[POINTER].body == before


def test_only_migrated_pages_may_leave_the_optional_legacy_dataset(mixed_release, monkeypatch):
    store, market, mirror, old_pages = mixed_release([("daily_bars", "2026-01-01")])
    before = {
        item.key: store.values[item.key].body for p in old_pages.partitions for item in p.objects
    }
    enable_native(monkeypatch, "daily_bars")
    with publication.prepare_market_lake(
        sqlite_path=market, mirror_root=mirror, store=store, as_of=DAY, release_id="native-only"
    ) as prepared:
        assert "jquants.source_pages" not in prepared.release.datasets
        publication.publish_prepared_market_lake(prepared, store=store)
    assert all(store.values[key].body == body for key, body in before.items())


def test_legacy_mode_keeps_the_original_mixed_floor(mixed_release):
    store, market, mirror, _ = mixed_release(
        [("daily_bars", "2026-01-01"), ("margin_alerts", "2026-02-01")]
    )
    with sqlite3.connect(market) as connection:
        connection.execute("DELETE FROM jquants_source_pages WHERE source='daily_bars'")
    with (
        pytest.raises(ValueError, match="coverage starts 2026-02-01"),
        publication.prepare_market_lake(
            sqlite_path=market, mirror_root=mirror, store=store, as_of=DAY, release_id="legacy"
        ),
    ):
        pytest.fail("legacy originals cannot disappear")


@pytest.mark.parametrize("corrupt", [False, True])
def test_floor_reads_exact_old_parquet_and_stops_at_first_remaining_month(
    mixed_release, monkeypatch, corrupt
):
    store, market, mirror, old_pages = mixed_release(
        [
            ("daily_bars", "2026-01-01"),
            ("daily_bars", "2026-02-01"),
            ("margin_alerts", "2026-02-02"),
            ("weekly_margin", "2026-03-01"),
        ]
    )
    objects = [item for part in old_pages.partitions for item in part.objects]
    for item in objects:
        (mirror / item.key).unlink()
    if corrupt:
        store.store_object(objects[1].key, b"wrong old source bytes")
    enable_native(monkeypatch, "daily_bars")
    store.get_keys.clear()
    if corrupt:
        with (
            pytest.raises(LakePublishError, match="digest mismatch"),
            publication.prepare_market_lake(
                sqlite_path=market, mirror_root=mirror, store=store, as_of=DAY, release_id="bad"
            ),
        ):
            pytest.fail("old floor needs digest-verified bytes")
    else:
        with publication.prepare_market_lake(
            sqlite_path=market, mirror_root=mirror, store=store, as_of=DAY, release_id="verified"
        ) as prepared:
            assert prepared.export.datasets["jquants.source_pages"].manifest.coverage_start == date(
                2026, 2, 2
            )
        assert objects[0].key in store.get_keys
        assert objects[1].key in store.get_keys
        assert objects[2].key not in store.get_keys
    assert not (mirror / objects[0].key).exists()
    assert not (mirror / objects[1].key).exists()
    # The unchanged March object was also produced by this export and must remain.
    assert (mirror / objects[2].key).exists()
    assert not list(mirror.glob(".source-history-*"))
