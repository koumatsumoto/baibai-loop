"""L1 acquisition and saved-input consumer operate with no previous market release."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import timedelta

import pytest
from tests.helpers.jquants_receipts import body
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.short_sale_reports_native import AT, DAY, attempt, records

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsPage,
    JQuantsRateLimitError,
)
from baibai_engine.data_collection.providers.jquants.short_sale_reports_native import (
    HEAD_KEY,
    PREFIX,
    ShortSaleReportsL1,
)
from baibai_engine.machine_analysis.generation.short_sale_reports_inputs import INPUT_PREFIX
from baibai_jobs.acquisition.jquants_short_sale_reports_native import (
    acquire_short_sale_reports_native,
)
from baibai_jobs.integration.short_sale_reports_native import fixed_input, materialize_saved_input


class Client:
    def __init__(self, *, at=AT, rows=None, limited=False, partial=False):
        self.at, self.rows, self.limited, self.partial = at, rows, limited, partial
        self.calls = []

    def close(self):
        pass

    def pages(self, source, params):
        self.calls.append((source, params))
        if self.limited:
            raise JQuantsRateLimitError("quota exhausted")
        yield JQuantsPage(
            source,
            params,
            self.at,
            body(
                records() if self.rows is None else self.rows,
                cursor="next" if self.partial else None,
            ),
        )
        if self.partial:
            raise JQuantsRateLimitError("quota exhausted after first page")


def test_native_acquire_sends_iso_dates_and_survives_without_l2(tmp_path):
    store = MemoryR2Store()
    ShortSaleReportsL1(store, tmp_path / "init").initialize()
    for n in range(2):
        client = Client(at=AT + timedelta(hours=n), rows=[])
        owner = ShortSaleReportsL1(store, tmp_path / str(n))
        report = acquire_short_sale_reports_native(
            owner, asof=DAY, start=DAY, client=client, clock=lambda client=client: client.at
        )
        assert client.calls == [("short_sale_reports", {"disc_date": "2026-11-02"})]
        assert report["changed" if n == 0 else "equal"] == 1
    assert len([key for key in store.values if key.startswith(PREFIX + "packs/")]) == 1
    assert all(key.startswith(PREFIX) for key in store.values)
    assert next(owner.fixed().requests(store, days=[DAY])).request.records() == []


@pytest.mark.parametrize("partial", [False, True])
def test_failure_is_preserved_and_halts_pass_without_marking_unattempted_days(tmp_path, partial):
    owner = ShortSaleReportsL1(MemoryR2Store(), tmp_path)
    owner.initialize()
    first = DAY - timedelta(days=1)
    client = Client(limited=not partial, partial=partial)
    report = acquire_short_sale_reports_native(
        owner, asof=DAY, start=first, client=client, clock=lambda: AT
    )
    assert report["failed"] == report["halted"] == 1
    assert len(client.calls) == 1
    assert set(owner.fixed().value()["scopes"]) == {first.isoformat()}
    retained = next(owner.fixed().requests(owner.store, days=[first]))
    assert retained.error is not None
    assert retained.first_query == {"disc_date": first.isoformat()}


def test_fixed_consumer_keeps_discdate_calcdate_and_original_identity(tmp_path):
    store = MemoryR2Store()
    owner = ShortSaleReportsL1(store, tmp_path / "raw")
    owner.initialize()
    yesterday = DAY - timedelta(days=1)
    # Same ticker and disclosure day, two calculation dates remain separate rows.
    owner.commit(
        [attempt("first", rows=[*records(calculated=yesterday), *records(calculated=DAY)])]
    )
    owner.commit(
        [
            attempt(
                "later",
                day=DAY + timedelta(days=1),
                at=AT + timedelta(hours=1),
                rows=records(DAY + timedelta(days=1), calculated=yesterday, ratio=0.008),
            )
        ]
    )
    saved = fixed_input(
        store,
        owner.fixed(),
        start=DAY,
        end=DAY + timedelta(days=1),
        evaluated_at=AT + timedelta(hours=2),
    )
    before = len(store.get_keys)
    output = tmp_path / "reader.sqlite"
    result = materialize_saved_input(store, saved, output=output)
    assert result["rows"] == 3
    assert result["coverage_status"] == "complete"
    assert HEAD_KEY not in store.get_keys[before:]
    with closing(sqlite3.connect(output)) as conn:
        assert conn.execute(
            "SELECT disclosed_at, calculated_at, short_ratio FROM jquants_short_sale_reports "
            "ORDER BY disclosed_at, source_ordinal"
        ).fetchall() == [
            (DAY.isoformat(), yesterday.isoformat(), 0.006),
            (DAY.isoformat(), DAY.isoformat(), 0.006),
            ((DAY + timedelta(days=1)).isoformat(), yesterday.isoformat(), 0.008),
        ]
    owner.commit([attempt("withdrawn", at=AT + timedelta(hours=3), rows=[])])
    assert materialize_saved_input(store, saved, output=tmp_path / "cold.sqlite")["rows"] == 3
    corrected = fixed_input(
        store,
        owner.fixed(),
        start=DAY,
        end=DAY + timedelta(days=1),
        evaluated_at=AT + timedelta(hours=4),
    )
    current = tmp_path / "corrected.sqlite"
    report = materialize_saved_input(store, corrected, output=current)
    assert report["rows"] == 1
    assert report["coverage_status"] == "complete"
    with closing(sqlite3.connect(current)) as conn:
        assert conn.execute(
            "SELECT disclosed_at, calculated_at FROM jquants_short_sale_reports"
        ).fetchall() == [((DAY + timedelta(days=1)).isoformat(), yesterday.isoformat())]
    with pytest.raises(FileExistsError):
        materialize_saved_input(store, saved, output=output)


def test_duplicate_reporter_rows_keep_provider_page_order_and_null_zero(tmp_path):
    store = MemoryR2Store()
    owner = ShortSaleReportsL1(store, tmp_path / "raw")
    owner.initialize()
    source = [*records(ratio=None), *records(ratio=None), *records(ratio=0)]
    source.append({**records(name="Reporter a")[0], "Notes": "withdrawn? not a flag"})
    original = attempt("paged", rows=source)
    page = original.pages[0]
    owner.commit(
        [
            replace(
                original,
                pages=(
                    replace(page, body=body(source[:1], cursor="next")),
                    replace(
                        page,
                        query={**page.query, "pagination_key": "next"},
                        body=body(source[1:]),
                    ),
                ),
            )
        ]
    )
    saved = fixed_input(store, owner.fixed(), start=DAY, end=DAY, evaluated_at=AT)
    output = tmp_path / "reader.sqlite"
    materialize_saved_input(store, saved, output=output)
    with closing(sqlite3.connect(output)) as conn:
        assert conn.execute(
            "SELECT source_ordinal, short_ratio, short_seller_name, notes "
            "FROM jquants_short_sale_reports ORDER BY source_ordinal"
        ).fetchall() == [
            (0, None, "Reporter A", None),
            (1, None, "Reporter A", None),
            (2, 0, "Reporter A", None),
            (3, 0.006, "Reporter a", "withdrawn? not a flag"),
        ]


def test_pubdate_is_not_an_alias_for_disclosure_date(tmp_path):
    owner = ShortSaleReportsL1(MemoryR2Store(), tmp_path / "raw")
    owner.initialize()
    record = records()[0]
    record["PubDate"] = record.pop("DiscDate")
    owner.commit([attempt("not-a-disclosure", rows=[record])])
    saved = fixed_input(owner.store, owner.fixed(), start=DAY, end=DAY, evaluated_at=AT)
    output = tmp_path / "reader.sqlite"
    report = materialize_saved_input(owner.store, saved, output=output)
    assert report["coverage_status"] == "partial"
    assert report["rows"] == 0


def test_partial_observation_and_failed_receipt_do_not_become_successful_empty(tmp_path):
    store = MemoryR2Store()
    owner = ShortSaleReportsL1(store, tmp_path / "raw")
    owner.initialize()
    owner.commit([attempt("partial-rows", rows=[*records(), {"DiscDate": DAY.isoformat()}])])
    saved = fixed_input(
        store, owner.fixed(), start=DAY, end=DAY + timedelta(days=1), evaluated_at=AT
    )
    output = tmp_path / "partial.sqlite"
    result = materialize_saved_input(store, saved, output=output)
    assert result["rows"] == 1
    assert result["partial_days"] == result["unavailable_days"] == 1
    # The L1 request is complete regardless of the projection's rejected row.
    assert owner.fixed().plan(start=DAY, end=DAY + timedelta(days=20), revision_overlap_days=0)[
        0
    ] == DAY + timedelta(days=1)
    owner.commit([attempt("failed", at=AT + timedelta(hours=1), partial=True)])
    later = fixed_input(
        store, owner.fixed(), start=DAY, end=DAY, evaluated_at=AT + timedelta(hours=2)
    )
    assert (
        materialize_saved_input(store, later, output=tmp_path / "failed.sqlite")["coverage_status"]
        == "partial"
    )


def test_normal_empty_is_covered_but_missing_is_not(tmp_path):
    store = MemoryR2Store()
    owner = ShortSaleReportsL1(store, tmp_path / "raw")
    owner.initialize()
    owner.commit([attempt("empty", rows=[])])
    saved = fixed_input(
        store, owner.fixed(), start=DAY, end=DAY + timedelta(days=1), evaluated_at=AT
    )
    output = tmp_path / "empty.sqlite"
    assert materialize_saved_input(store, saved, output=output)["rows"] == 0
    with closing(sqlite3.connect(output)) as conn:
        assert conn.execute(
            "SELECT coverage_start, status, record_count FROM source_coverage "
            "WHERE source='jquants_short_sale_reports' ORDER BY coverage_start"
        ).fetchall() == [
            (DAY.isoformat(), "ok", 0),
            ((DAY + timedelta(days=1)).isoformat(), "unavailable", 0),
        ]


def test_monthly_fixed_input_reuses_old_metadata_and_no_whole_history_body(tmp_path):
    store = MemoryR2Store()
    owner = ShortSaleReportsL1(store, tmp_path / "raw")
    owner.initialize()
    old_day = DAY.replace(month=9, day=1)
    owner.commit([attempt("old", day=old_day), attempt("new")])
    saved = fixed_input(store, owner.fixed(), start=old_day, end=DAY, evaluated_at=AT)
    months = {key for key in store.values if key.startswith(INPUT_PREFIX + "months/")}
    assert len(months) == 2
    before = len(store.put_keys)
    owner.commit([attempt("equal", at=AT + timedelta(hours=1))])
    newer = fixed_input(
        store, owner.fixed(), start=old_day, end=DAY, evaluated_at=AT + timedelta(hours=2)
    )
    new_months = [
        key for key in store.put_keys[before:] if key.startswith(INPUT_PREFIX + "months/")
    ]
    assert len(new_months) == 1
    assert saved.sha256 != newer.sha256
    assert len(json.dumps(saved.descriptor)) < 1500
    assert "scopes" not in saved.descriptor
    store.values.pop(HEAD_KEY)
    assert (
        materialize_saved_input(
            store, saved, output=tmp_path / "old.sqlite", start=old_day, end=old_day
        )["rows"]
        == 1
    )


def test_saved_input_rejects_future_clock_and_missing_pack(tmp_path):
    store = MemoryR2Store()
    owner = ShortSaleReportsL1(store, tmp_path / "raw")
    owner.initialize()
    owner.commit([attempt("first")])
    with pytest.raises(ValueError, match="evaluation clock"):
        fixed_input(
            store, owner.fixed(), start=DAY, end=DAY, evaluated_at=AT - timedelta(seconds=1)
        )
    saved = fixed_input(store, owner.fixed(), start=DAY, end=DAY, evaluated_at=AT)
    for key in list(store.values):
        if key.startswith(PREFIX + "packs/"):
            del store.values[key]
    with pytest.raises(Exception, match="unavailable"):
        materialize_saved_input(store, saved, output=tmp_path / "bad.sqlite")
    assert not (tmp_path / "bad.sqlite").exists()


def test_cli_end_to_end_uses_saved_input_after_head_disappears(tmp_path, monkeypatch):
    from baibai_jobs import cli
    from baibai_jobs.acquisition import jquants_short_sale_reports_native as acquisition
    from baibai_jobs.integration import short_sale_reports_native as integration

    class Store(MemoryR2Store):
        def close(self):
            pass

    store = Store()
    monkeypatch.setattr(acquisition, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(integration, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(acquisition, "JQuantsHTTP", lambda _: Client())
    directory = str(tmp_path / "work")
    assert (
        cli.main(["jquants-short-sale-reports-native", "initialize", "--directory", directory]) == 0
    )
    assert (
        cli.main(
            [
                "jquants-short-sale-reports-native",
                "plan",
                "--directory",
                directory,
                "--asof",
                DAY.isoformat(),
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "jquants-short-sale-reports-native",
                "acquire",
                "--directory",
                directory,
                "--start",
                DAY.isoformat(),
                "--asof",
                DAY.isoformat(),
            ]
        )
        == 0
    )
    assert (
        cli.main(["jquants-short-sale-reports-native", "commit-work", "--directory", directory])
        == 0
    )
    output = tmp_path / "out"
    args = [
        "rebuild-short-sale-reports-native",
        "--work-directory",
        directory,
        "--start",
        DAY.isoformat(),
        "--end",
        DAY.isoformat(),
    ]
    assert cli.main([*args, "--output", str(output)]) == 0
    store.values.pop(HEAD_KEY)
    assert (
        cli.main(
            [*args, "--saved-input", str(output / "input.json"), "--output", str(tmp_path / "cold")]
        )
        == 0
    )
    with closing(sqlite3.connect(tmp_path / "cold" / "short-sale-reports.sqlite")) as conn:
        assert conn.execute("SELECT disclosed_at FROM jquants_short_sale_reports").fetchall() == [
            (DAY.isoformat(),)
        ]
