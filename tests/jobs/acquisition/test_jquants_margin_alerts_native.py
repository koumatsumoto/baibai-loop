"""L1 acquisition and saved-input consumer operate with no previous market release."""

import json
from datetime import timedelta

import pytest
from tests.helpers.jquants_receipts import body
from tests.helpers.margin_alerts_native import AT, DAY, attempt, records
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsPage,
    JQuantsRateLimitError,
)
from baibai_engine.data_collection.providers.jquants.margin_alerts_native import (
    HEAD_KEY,
    PREFIX,
    MarginAlertsL1,
)
from baibai_engine.machine_analysis.generation.margin_alerts_inputs import INPUT_PREFIX
from baibai_engine.machine_analysis.observations.trading_activity.margin_reader import (
    read_margin_alerts,
)
from baibai_jobs.acquisition.jquants_margin_alerts_native import acquire_margin_alerts_native
from baibai_jobs.integration.margin_alerts_native import fixed_input, materialize_saved_input


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
    MarginAlertsL1(store, tmp_path / "init").initialize()
    for n in range(2):
        client = Client(at=AT + timedelta(hours=n), rows=[])
        owner = MarginAlertsL1(store, tmp_path / str(n))
        report = acquire_margin_alerts_native(
            owner, asof=DAY, start=DAY, client=client, clock=lambda client=client: client.at
        )
        assert client.calls == [("margin_alerts", {"date": "2026-11-02"})]
        assert report["changed" if n == 0 else "equal"] == 1
    assert len([key for key in store.values if key.startswith(PREFIX + "packs/")]) == 1
    assert all(key.startswith(PREFIX) for key in store.values)
    assert next(owner.fixed().requests(store, days=[DAY])).request.records() == []


@pytest.mark.parametrize("partial", [False, True])
def test_failure_is_preserved_and_halts_pass_without_marking_unattempted_days(tmp_path, partial):
    owner = MarginAlertsL1(MemoryR2Store(), tmp_path)
    owner.initialize()
    first = DAY - timedelta(days=1)
    client = Client(limited=not partial, partial=partial)
    report = acquire_margin_alerts_native(
        owner, asof=DAY, start=first, client=client, clock=lambda: AT
    )
    assert report["failed"] == report["halted"] == 1
    assert len(client.calls) == 1
    assert set(owner.fixed().value()["scopes"]) == {first.isoformat()}
    assert next(owner.fixed().requests(owner.store, days=[first])).error is not None


def test_fixed_consumer_keeps_pubdate_appdate_subset_and_original_identity(tmp_path):
    store = MemoryR2Store()
    owner = MarginAlertsL1(store, tmp_path / "raw")
    owner.initialize()
    yesterday = DAY - timedelta(days=1)
    # Same ticker and publication, two different application dates are distinct.
    owner.commit(
        [attempt("first", rows=[*records(application=yesterday), *records(application=DAY)])]
    )
    owner.commit(
        [
            attempt(
                "later",
                day=DAY + timedelta(days=1),
                at=AT + timedelta(hours=1),
                rows=records(DAY + timedelta(days=1), application=yesterday, long=2400),
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
    rows = read_margin_alerts(output, DAY, DAY + timedelta(days=1))
    assert len(rows) == 3
    assert [(row.publication_date, row.applied_date) for row in rows] == [
        (DAY, yesterday),
        (DAY, DAY),
        (DAY + timedelta(days=1), yesterday),
    ]
    assert rows[-1].long_outstanding == 2400
    owner.commit([attempt("withdrawn", at=AT + timedelta(hours=3), rows=[])])
    assert materialize_saved_input(store, saved, output=tmp_path / "cold.sqlite")["rows"] == 3
    with pytest.raises(FileExistsError):
        materialize_saved_input(store, saved, output=output)


def test_partial_observation_and_failed_receipt_do_not_become_successful_empty(tmp_path):
    store = MemoryR2Store()
    owner = MarginAlertsL1(store, tmp_path / "raw")
    owner.initialize()
    owner.commit([attempt("partial-rows", rows=[*records(), {"PubDate": DAY.isoformat()}])])
    saved = fixed_input(
        store, owner.fixed(), start=DAY, end=DAY + timedelta(days=1), evaluated_at=AT
    )
    output = tmp_path / "partial.sqlite"
    result = materialize_saved_input(store, saved, output=output)
    assert result["rows"] == 1
    assert result["partial_days"] == result["unavailable_days"] == 1
    assert read_margin_alerts(output, DAY, DAY) is None
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


def test_normal_empty_is_readable_and_missing_is_not(tmp_path):
    store = MemoryR2Store()
    owner = MarginAlertsL1(store, tmp_path / "raw")
    owner.initialize()
    owner.commit([attempt("empty", rows=[])])
    saved = fixed_input(
        store, owner.fixed(), start=DAY, end=DAY + timedelta(days=1), evaluated_at=AT
    )
    output = tmp_path / "empty.sqlite"
    materialize_saved_input(store, saved, output=output)
    assert read_margin_alerts(output, DAY, DAY) == []
    assert read_margin_alerts(output, DAY + timedelta(days=1), DAY + timedelta(days=1)) is None


def test_monthly_fixed_input_reuses_old_metadata_and_no_whole_history_body(tmp_path):
    store = MemoryR2Store()
    owner = MarginAlertsL1(store, tmp_path / "raw")
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
    owner = MarginAlertsL1(store, tmp_path / "raw")
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
    from baibai_jobs.acquisition import jquants_margin_alerts_native as acquisition
    from baibai_jobs.integration import margin_alerts_native as integration

    class Store(MemoryR2Store):
        def close(self):
            pass

    store = Store()
    monkeypatch.setattr(acquisition, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(integration, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(acquisition, "JQuantsHTTP", lambda _: Client())
    directory = str(tmp_path / "work")
    assert cli.main(["jquants-margin-alerts-native", "initialize", "--directory", directory]) == 0
    assert (
        cli.main(
            [
                "jquants-margin-alerts-native",
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
                "jquants-margin-alerts-native",
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
    assert cli.main(["jquants-margin-alerts-native", "commit-work", "--directory", directory]) == 0
    output = tmp_path / "out"
    args = [
        "rebuild-margin-alerts-native",
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
    assert len(read_margin_alerts(tmp_path / "cold" / "margin-alerts.sqlite", DAY, DAY)) == 1
