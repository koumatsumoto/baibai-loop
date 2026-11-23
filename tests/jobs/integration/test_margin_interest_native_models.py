"""The two margin datasets share fixed originals, calendar, execution seal and lineage."""

from contextlib import closing
from datetime import date, timedelta

import pytest
from tests.helpers.margin_interest_native import AT, DAY, attempt, calendar, records
from tests.helpers.master_calendar_native import (
    native_master_calendar_publication as native_master_calendar_publication,
)
from tests.helpers.price_native import native_price_publication as native_price_publication
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.calendar_native import (
    HEAD_KEY as CALENDAR_HEAD,
)
from baibai_engine.data_collection.providers.jquants.margin_interest_native import (
    HEAD_KEY,
    PREFIX,
    MarginInterestL1,
)
from baibai_engine.machine_analysis.generation.margin_interest_inputs import (
    INPUT_PREFIX,
)
from baibai_engine.storage.snapshots.writer import sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration import margin_interest_native as projection
from baibai_jobs.publication.l1_margin_interest_export import (
    export_margin_interest_from_native_input,
)

WEEK = date(2026, 9, 18)
NAMES = ("jquants.weekly_margin", "jquants.all_issues_daily_margin")


def setup(tmp_path, monkeypatch):
    # Isolate the connection; the unmodified planner suite covers the 230-day window.
    monkeypatch.setattr(projection, "WEEKLY_BALANCE_LOOKBACK_DAYS", 7)
    store = MemoryR2Store()
    owner = MarginInterestL1(store, tmp_path / "raw")
    owner.initialize()
    owner.commit(
        [attempt("weekly", day=WEEK), attempt("daily", rows=records(publication=date(2026, 9, 28)))]
    )
    cal = calendar(store, tmp_path / "calendar", WEEK, DAY)
    market, mirror = tmp_path / "market.sqlite", tmp_path / "mirror"
    with closing(open_connection(market)):
        pass
    return store, owner, cal, market, mirror


def rows(path):
    with closing(open_connection(path)) as connection:
        return tuple(
            connection.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
            for table in ("jquants_weekly_margin", "jquants_all_issues_daily_margin")
        )


def coverage(path):
    with closing(open_connection(path)) as connection:
        return connection.execute(
            "SELECT * FROM source_coverage WHERE source IN "
            "('jquants_weekly_margin','jquants_all_issues_daily_margin') ORDER BY source,coverage_key"
        ).fetchall()


def build(store, fixed, cal, market, mirror, *, at=AT, previous=None):
    saved = projection.model_input(
        store,
        fixed,
        cal,
        as_of=DAY,
        evaluated_at=at,
        market=market,
        mirror_root=mirror,
        previous=previous,
    )
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=mirror) as sealed:
        reports = export_margin_interest_from_native_input(
            source_snapshot=sealed, saved=saved, mirror_root=mirror, producer_git_commit="a" * 40
        )
    for report in reports.values():
        for part in report.manifest.partitions:
            for obj in part.objects:
                store.store_object(obj.key, (mirror / obj.key).read_bytes(), sha256=obj.sha256)
    return saved, {name: report.manifest for name, report in reports.items()}


@pytest.mark.parametrize("prior_usable", [True, False])
def test_equal_confirmation_reuses_both_proofs_or_recomputes_from_originals(
    tmp_path, monkeypatch, capsys, prior_usable
):
    store, owner, cal, market, mirror = setup(tmp_path, monkeypatch)
    prior_saved, previous = build(store, owner.fixed(), cal, market, mirror)
    prior_selection = prior_saved.descriptor["selection_sha256"]
    prior_key = f"{INPUT_PREFIX}selections/{prior_selection}.json"
    if not prior_usable:
        store.store_object(prior_key, b"{}", sha256=prior_selection)
    owner.commit(
        [attempt("equal", at=AT + timedelta(hours=1), rows=records(publication=date(2026, 9, 28)))]
    )
    fixed = owner.fixed()
    store.get_keys.clear()
    saved, published = build(
        store, fixed, cal, market, mirror, at=AT + timedelta(hours=1), previous=previous
    )
    diagnostics = [
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("native margin interest:")
    ]
    assert len(diagnostics) == 2
    for line in diagnostics:
        times = dict(field.split("=") for field in line.split()[3:7])
        assert set(times) == {
            "fixed_input_seconds",
            "prior_projection_seconds",
            "calendar_replay_seconds",
            "project_balances_seconds",
        }
        assert all(float(value) >= 0 for value in times.values())
    assert diagnostics[0].endswith("adopted_days=2 reused_days=0")
    assert diagnostics[1].endswith(f"adopted_days=2 reused_days={2 if prior_usable else 0}")
    assert any(key.startswith(PREFIX + "packs/") for key in store.get_keys) != prior_usable
    assert HEAD_KEY not in store.get_keys
    assert store.get_keys.count(prior_key) == 2
    assert all(manifest.coverage_status == "complete" for manifest in published.values())
    assert all(
        {obj.key for part in previous[name].partitions for obj in part.objects}
        == {obj.key for part in published[name].partitions for obj in part.objects}
        for name in NAMES
    )
    cold = tmp_path / "cold.sqlite"
    projection.materialize_saved_input(store, saved, output=cold)
    assert rows(cold) == rows(market)
    assert coverage(cold) == coverage(market)


@pytest.mark.parametrize("boolean", [True, False])
@pytest.mark.parametrize("balance", [WEEK, DAY])
def test_old_boolean_projection_cannot_reuse_weekly_or_daily_rows(
    tmp_path, monkeypatch, boolean, balance
):
    from baibai_engine.machine_analysis.observations import jquants_writer
    from baibai_jobs.publication import l1_margin_interest_export as exporter

    store, owner, cal, market, mirror = setup(tmp_path, monkeypatch)
    publication = date(2026, 9, 28) if balance == DAY else None
    good = {**records(balance, publication=publication)[0], "Code": "72030"}
    owner.commit(
        [
            attempt(
                "boolean",
                day=balance,
                at=AT + timedelta(hours=1),
                rows=[*records(balance, long=boolean, publication=publication), good],
            )
        ]
    )
    fixed = owner.fixed()
    current_float = jquants_writer.to_float
    with monkeypatch.context() as old:
        old.setattr(
            jquants_writer,
            "to_float",
            lambda value: float(value) if isinstance(value, bool) else current_float(value),
        )
        old.setattr(projection, "JQUANTS_NUMERIC_PROJECTION_REVISION", "jquants-numeric-v1")
        # Historical producer fixture: its Parquet footer had no numeric revision.
        old.setattr(exporter, "require_current_projection_revision", lambda _summary: None)
        before, previous = build(store, fixed, cal, market, mirror, at=AT + timedelta(hours=1))
    assert all(manifest.coverage_status == "complete" for manifest in previous.values())
    old_rows = rows(market)
    saved, _ = build(
        store, fixed, cal, market, mirror, at=AT + timedelta(hours=1), previous=previous
    )
    cold = tmp_path / "cold.sqlite"
    projection.materialize_saved_input(store, saved, output=cold)
    assert rows(market) == rows(cold)
    assert coverage(market) == coverage(cold)
    changed = 0 if balance == WEEK else 1
    assert rows(market)[changed] != old_rows[changed]
    assert len(rows(market)[changed]) == (2 if balance == WEEK else 1)
    assert saved.descriptor["summary"]["coverage_status"] == (
        "complete" if balance == WEEK else "partial"
    )
    assert saved.descriptor["selection_sha256"] == before.descriptor["selection_sha256"]
    assert saved.sha256 != before.sha256


@pytest.mark.parametrize("damage", ["row", "absent", "object", "partial"])
def test_old_l2_is_optional_and_latest_cannot_change_fixed_input(tmp_path, monkeypatch, damage):
    store, owner, cal, market, mirror = setup(tmp_path, monkeypatch)
    fixed = owner.fixed()
    saved, previous = build(store, fixed, cal, market, mirror)
    expected = rows(market)
    if damage in {"row", "absent"}:
        with closing(open_connection(market)) as connection, connection:
            connection.execute(
                "UPDATE jquants_all_issues_daily_margin SET long_vol=999"
                if damage == "row"
                else "DELETE FROM jquants_weekly_margin"
            )
    elif damage == "object":
        for manifest in previous.values():
            for part in manifest.partitions:
                for obj in part.objects:
                    (mirror / obj.key).unlink()
                    store.values.pop(obj.key)
    else:
        previous = {
            name: manifest.model_copy(update={"coverage_status": "partial"})
            for name, manifest in previous.items()
        }
    owner.commit([attempt("correction", at=AT + timedelta(hours=1), rows=records(long=999))])
    store.values.pop(HEAD_KEY)
    store.values.pop(CALENDAR_HEAD)
    store.get_keys.clear()
    rebuilt = projection.model_input(
        store,
        fixed,
        cal,
        as_of=DAY,
        evaluated_at=AT,
        market=market,
        mirror_root=mirror,
        previous=previous,
    )
    assert rebuilt == saved
    assert rows(market) == expected
    assert HEAD_KEY not in store.get_keys
    assert CALENDAR_HEAD not in store.get_keys
    assert any(key.startswith(PREFIX + "packs/") for key in store.get_keys)


def test_unavailable_and_partial_sources_never_keep_unproven_rows(tmp_path, monkeypatch):
    store, owner, cal, market, mirror = setup(tmp_path, monkeypatch)
    _, previous = build(store, owner.fixed(), cal, market, mirror)
    expected = rows(market)
    owner.commit([attempt("interrupted", at=AT + timedelta(hours=1), partial=True)])
    saved = projection.model_input(
        store,
        owner.fixed(),
        cal,
        as_of=DAY,
        evaluated_at=AT + timedelta(hours=1),
        market=market,
        mirror_root=mirror,
        previous=previous,
    )
    assert rows(market) == expected
    assert saved.descriptor["summary"]["daily_coverage_status"] == "partial"
    assert saved.descriptor["summary"]["weekly_coverage_status"] == "complete"
    cold = tmp_path / "failed.sqlite"
    projection.materialize_saved_input(store, saved, output=cold)
    assert rows(cold) == rows(market)
    assert coverage(cold) == coverage(market)
    owner.commit([attempt("empty", at=AT + timedelta(hours=2), rows=[])])
    saved = projection.model_input(
        store,
        owner.fixed(),
        cal,
        as_of=DAY,
        evaluated_at=AT + timedelta(hours=2),
        market=market,
        mirror_root=mirror,
        previous=previous,
    )
    assert rows(market)[1] == []
    assert saved.descriptor["summary"]["daily_coverage_status"] == "partial"


def test_margin_projection_keeps_calendar_outside_selected_balances(tmp_path, monkeypatch):
    store, owner, cal, market, mirror = setup(tmp_path, monkeypatch)
    with closing(open_connection(market)) as connection, connection:
        connection.execute("INSERT INTO jquants_market_calendar VALUES ('2027-01-01','0')")
    projection.model_input(
        store, owner.fixed(), cal, as_of=DAY, evaluated_at=AT, market=market, mirror_root=mirror
    )
    with closing(open_connection(market)) as connection:
        assert connection.execute(
            "SELECT holiday_division FROM jquants_market_calendar WHERE day='2027-01-01'"
        ).fetchone() == ("0",)


def test_publication_shares_fixed_margin_calendar_and_seal(tmp_path, monkeypatch):
    from tests.helpers.jquants_receipts import body
    from tests.helpers.lake_policy import narrow_release_policy
    from tests.helpers.master_calendar_native import seed_master

    from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
    from baibai_engine.data_collection.providers.jquants.price_native import PriceAttempt, PriceL1
    from baibai_jobs.publication import snapshot_publication as publication
    from baibai_jobs.publication.datasets import LAKE_DATASETS

    store, owner, cal, market, mirror = setup(tmp_path, monkeypatch)
    seed_master(store, market, mirror / "master-native", as_of=DAY)
    prices = PriceL1(store, mirror / "price-native")
    prices.initialize()
    originals = []
    for offset in range((DAY - WEEK).days + 1):
        day = WEEK + timedelta(days=offset)
        if day.weekday() >= 5:  # No provider request on the two known closed days.
            continue
        text = day.isoformat()
        page = JQuantsPage(
            "daily_bars", {"date": text}, AT, body([{"Date": text, "Code": "13010", "C": 100}])
        )
        originals.append(PriceAttempt("price-" + text, day, AT, (page,)))
    prices.commit(originals)
    datasets = (*NAMES, "jquants.market_calendar", "jquants.daily_bars", "jquants.master_snapshots")
    assert set(datasets) <= {
        item.dataset for item in publication.release_policy_for_profile("production").datasets
    }
    narrow_release_policy(monkeypatch, datasets=datasets)
    monkeypatch.setattr(
        publication, "LAKE_DATASETS", {name: LAKE_DATASETS[name] for name in datasets}
    )
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)
    store.get_keys.clear()
    with publication.prepare_market_lake(
        sqlite_path=market, mirror_root=mirror, store=store, as_of=DAY
    ) as prepared:
        assert prepared.price_identity.descriptor["summary"]["closed_unrequested_days"] == 2
        # Calendar, price closures and overlapping credit consume the same original once.
        assert len([key for key in store.get_keys if "market-calendar/v1/packs/" in key]) == 1
        saved = prepared.margin_interest_identity
        assert saved is not None
        assert (
            saved.descriptor["summary"]["calendar_input"]["descriptor"]["head_sha256"]
            == cal.content_id
        )
        expected = rows(prepared.source_snapshot.path)
        assert expected == rows(market)
        for name in NAMES:
            manifest = prepared.export.datasets[name].manifest
            assert {source.sha256 for part in manifest.partitions for source in part.sources} == {
                saved.descriptor["selection_sha256"]
            }
        owner.commit([attempt("later", at=AT + timedelta(hours=2), rows=records(long=999))])
        store.get_keys.clear()
        publication.publish_prepared_market_lake(prepared, store=store)
        assert HEAD_KEY not in store.get_keys
        assert rows(prepared.source_snapshot.path) == expected
    cold = tmp_path / "published-cold.sqlite"
    projection.materialize_saved_input(store, saved, output=cold)
    assert rows(cold) == expected


pytestmark = pytest.mark.usefixtures(
    "native_price_publication", "native_master_calendar_publication"
)
