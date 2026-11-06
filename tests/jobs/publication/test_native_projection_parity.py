"""同じsealのnative exportは一度検証し、変換値の差異は公開前に拒否する。"""

from collections import Counter

import pytest
from tests.engine.data_collection.providers.jquants.test_calendar_native import DAY, attempt, owner
from tests.helpers.lake_release import market_store
from tests.jobs.publication.test_calendar_native_objects import configure, native_dependencies

from baibai_engine.storage.snapshots import writer
from baibai_engine.storage.snapshots.writer import LakeBuildError

NATIVE_NAMES = (
    "jquants.daily_bars",
    "jquants.market_calendar",
    "jquants.master_snapshots",
)


def setup_inputs(tmp_path, monkeypatch):
    publication = configure(monkeypatch)
    store, calendar = owner(tmp_path / "calendar")
    calendar.commit([attempt("calendar")])
    native_dependencies(store, tmp_path)
    market = market_store(tmp_path / "market.sqlite")
    return publication, store, market


def test_same_seal_native_exports_are_fully_checked_once(tmp_path, monkeypatch):
    publication, store, market = setup_inputs(tmp_path, monkeypatch)
    original = writer.validate_snapshot_parity
    checks = []

    def record(**kwargs):
        checks.append((kwargs["dataset"].name, kwargs["sqlite_path"]))
        return original(**kwargs)

    monkeypatch.setattr(writer, "validate_snapshot_parity", record)
    monkeypatch.setattr(publication, "validate_snapshot_parity", record)
    originals = {key: value.body for key, value in store.values.items()}
    with publication.prepare_market_lake(
        sqlite_path=market,
        mirror_root=tmp_path / "mirror",
        store=store,
        as_of=DAY,
        release_id="native-once",
    ) as prepared:
        assert Counter(name for name, _ in checks) == Counter(NATIVE_NAMES)
        assert {path for _, path in checks} == {prepared.source_snapshot.path}
        assert set(prepared.export.datasets) == set(NATIVE_NAMES)
    assert all(store.values[key].body == body for key, body in originals.items())
    assert "lake/pointers/market/current.json" not in store.values


@pytest.mark.parametrize(
    ("name", "column", "wrong"),
    [
        ("jquants.daily_bars", "close", 999.0),
        ("jquants.market_calendar", "holiday_division", "0"),
        ("jquants.master_snapshots", "record_json", "{}"),
    ],
)
def test_native_value_difference_is_rejected_by_exporter(
    tmp_path, monkeypatch, name, column, wrong
):
    publication, store, market = setup_inputs(tmp_path, monkeypatch)
    build = writer._build_period

    def changed_build(**kwargs):
        dataset, rows = kwargs["dataset"], kwargs["rows"]
        if dataset.name == name:
            index = tuple(item.name for item in dataset.columns).index(column)

            def changed_rows():
                for number, row in enumerate(rows):
                    if number == 0:
                        row = list(row)
                        assert row[index] != wrong
                        row[index] = wrong
                    yield tuple(row)

            kwargs["rows"] = changed_rows()
        return build(**kwargs)

    monkeypatch.setattr(writer, "_build_period", changed_build)
    originals = {key: value.body for key, value in store.values.items()}
    with (
        pytest.raises(LakeBuildError, match="values or row order differ"),
        publication.prepare_market_lake(
            sqlite_path=market,
            mirror_root=tmp_path / "mirror",
            store=store,
            as_of=DAY,
            release_id="native-difference",
        ),
    ):
        pytest.fail("different derived values must not be published")
    assert all(store.values[key].body == body for key, body in originals.items())
    assert "lake/pointers/market/current.json" not in store.values
