"""L1-only multiple cycles and disposable L2 through the real entry functions."""

import json
from datetime import UTC, date, datetime, timedelta

import pyarrow.parquet as pq
import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsAcquisitionError,
    JQuantsPage,
)
from baibai_engine.data_collection.providers.jquants.financial_native import FinancialL1
from baibai_jobs.acquisition.jquants_financial_native import acquire_financial_native
from baibai_jobs.integration.financial_native import rebuild

DAY = date(2026, 11, 1)
AT = datetime(2026, 11, 2, tzinfo=UTC)


class Client:
    def __init__(self, at, value=1, partial=False):
        self.at, self.value, self.partial = at, value, partial
        self.calls = []

    def pages(self, source, params):
        self.calls.append((source, params))
        row = {
            "Code": "13010",
            "DiscDate": date.fromisoformat(params["date"]).isoformat(),
            "DiscNo": "1",
            "Sales": self.value,
        }
        yield JQuantsPage(
            source,
            params,
            self.at,
            json.dumps(
                {"data": [row], **({"pagination_key": "next"} if self.partial else {})}
            ).encode(),
        )
        if self.partial:
            raise JQuantsAcquisitionError("no second page")


def test_multiple_cycles_without_l2_then_delete_and_rebuild(tmp_path):
    store = MemoryR2Store()
    FinancialL1(store, tmp_path / "init").initialize()
    snapshots = []
    for cycle, value in enumerate((1, 1, 2)):
        owner = FinancialL1(store, tmp_path / str(cycle))
        client = Client(AT + timedelta(hours=cycle), value)
        result = acquire_financial_native(
            owner,
            start=DAY,
            end=DAY,
            revision_overlap_days=0,
            client=client,
            clock=lambda client=client: client.at,
            sleep=lambda _: None,
        )
        assert result["equal" if cycle == 1 else "changed"] == 1
        assert len(client.calls) == 1
        snapshots.append(owner.fixed())
    assert (
        snapshots[1].value()["scopes"][DAY.isoformat()]["confirmed_at"]
        > snapshots[0].value()["scopes"][DAY.isoformat()]["confirmed_at"]
    )
    first = tmp_path / "first"
    rebuild(store, snapshots[0], start=DAY, end=DAY, output=first)
    latest = tmp_path / "latest"
    rebuild(store, snapshots[2], start=DAY, end=DAY, output=latest)
    rows = pq.read_table(latest / "financial_summaries.parquet").to_pylist()
    assert json.loads(rows[0]["document_payload"])["Sales"] == 2
    assert (
        json.loads(
            pq.read_table(first / "financial_summaries.parquet").to_pylist()[0]["document_payload"]
        )["Sales"]
        == 1
    )
    assert (
        pq.read_table(latest / "financial_receipt_status.parquet").to_pylist()[0][
            "l1_coverage_status"
        ]
        == "complete"
    )
    for path in latest.iterdir():
        path.unlink()
    latest.rmdir()
    rebuild(store, snapshots[2], start=DAY, end=DAY, output=latest)
    assert pq.read_table(latest / "financial_summaries.parquet").to_pylist() == rows
    assert json.loads((latest / "input.json").read_text())["head_sha256"] == snapshots[2].content_id


def test_projection_failure_does_not_undo_l1_or_block_next_cycle(tmp_path, monkeypatch):
    import baibai_jobs.integration.financial_native as projection

    store = MemoryR2Store()
    owner = FinancialL1(store, tmp_path / "one")
    owner.initialize()
    acquire_financial_native(
        owner, start=DAY, end=DAY, revision_overlap_days=0, client=Client(AT), clock=lambda: AT
    )
    fixed = owner.fixed()
    monkeypatch.setattr(
        projection,
        "rebuild_financial_days",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("L2 fails")),
    )
    with pytest.raises(ValueError, match="L2 fails"):
        rebuild(store, fixed, start=DAY, end=DAY, output=tmp_path / "failed")
    next_owner = FinancialL1(store, tmp_path / "two")
    result = acquire_financial_native(
        next_owner,
        start=DAY,
        end=DAY,
        revision_overlap_days=0,
        client=Client(AT + timedelta(hours=1)),
        clock=lambda: AT + timedelta(hours=1),
    )
    assert result["equal"] == 1
    assert (
        next_owner.fixed().value()["scopes"][DAY.isoformat()]["adopted"]
        == fixed.value()["scopes"][DAY.isoformat()]["adopted"]
    )


def test_partial_pages_remain_l1_and_rebuild_reports_failure(tmp_path):
    store = MemoryR2Store()
    owner = FinancialL1(store, tmp_path / "work")
    owner.initialize()
    report = acquire_financial_native(
        owner,
        start=DAY,
        end=DAY,
        revision_overlap_days=0,
        client=Client(AT, partial=True),
        clock=lambda: AT,
    )
    assert report["failed"] == 1
    assert len(list(owner.directory.glob("attempt-*.json"))) == 1
    output = tmp_path / "rebuild"
    rebuild(store, owner.fixed(), start=DAY, end=DAY, output=output)
    row = pq.read_table(output / "financial_receipt_status.parquet").to_pylist()[0]
    assert row["projection_status"] == "unavailable"
    assert row["incomplete_requests"] == 1
    assert row["l1_coverage_status"] == "partial"


def test_cli_dispatch_acquires_commits_retries_and_rebuilds(tmp_path, monkeypatch, capsys):
    from baibai_jobs import cli
    from baibai_jobs.acquisition import jquants_financial_native as acquisition
    from baibai_jobs.integration import financial_native as integration

    class Store(MemoryR2Store):
        def close(self):
            pass

    class ClosingClient(Client):
        def close(self):
            pass

    store = Store()
    monkeypatch.setattr(acquisition, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(integration, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(acquisition, "JQuantsHTTP", lambda _: ClosingClient(AT))
    work = str(tmp_path / "acquire")
    assert cli.main(["jquants-financial-native", "initialize", "--directory", work]) == 0
    assert (
        cli.main(
            [
                "jquants-financial-native",
                "plan",
                "--directory",
                work,
                "--start",
                DAY.isoformat(),
                "--end",
                DAY.isoformat(),
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "jquants-financial-native",
                "acquire",
                "--directory",
                work,
                "--start",
                DAY.isoformat(),
                "--end",
                DAY.isoformat(),
            ]
        )
        == 0
    )
    assert cli.main(["jquants-financial-native", "commit-work", "--directory", work]) == 0
    assert '"retried"' in capsys.readouterr().out
    assert (
        cli.main(
            [
                "rebuild-financial-native",
                "--work-directory",
                str(tmp_path / "read"),
                "--start",
                DAY.isoformat(),
                "--end",
                DAY.isoformat(),
                "--output",
                str(tmp_path / "output"),
            ]
        )
        == 0
    )
    assert (tmp_path / "output" / "input.json").is_file()
    assert cli.main(["jquants-financial-native", "plan", "--directory", work]) == 1


@pytest.mark.parametrize("kind", ["no-pages", "partial", "held"])
def test_commit_work_retry_keeps_current_unresolved_scope_failed(
    tmp_path, monkeypatch, capsys, kind
):
    from baibai_engine.data_collection.providers.jquants.financial_native import FinancialAttempt
    from baibai_jobs.acquisition import jquants_financial_native as acquisition

    class Store(MemoryR2Store):
        def close(self):
            pass

    store = Store()
    owner = FinancialL1(store, tmp_path / "work")
    owner.initialize()
    monkeypatch.setattr(acquisition, "Boto3R2Store", lambda **_: store)
    if kind == "held":
        original = FinancialAttempt(
            "original",
            DAY,
            AT,
            tuple(Client(AT).pages("financial_summaries", {"date": DAY.isoformat()})),
        )
        owner.commit([original])
        item = FinancialAttempt(
            "held",
            DAY,
            AT,
            tuple(Client(AT, value=2).pages("financial_summaries", {"date": DAY.isoformat()})),
        )
    else:
        pages = (
            ()
            if kind == "no-pages"
            else (
                JQuantsPage(
                    "financial_summaries",
                    {"date": DAY.isoformat()},
                    AT,
                    b'{"data": [], "pagination_key": "next"}',
                ),
            )
        )
        item = FinancialAttempt("failed", DAY, AT, pages, "provider_failure")
    (owner.directory / "attempt-saved.json").write_text(json.dumps(item.value()))
    args = ["commit-work", "--directory", str(owner.directory)]
    assert acquisition.main(args) == 1
    capsys.readouterr()
    writes = len(store.put_keys)
    assert acquisition.main(args) == 1
    report = json.loads(capsys.readouterr().out)
    reason = "held" if kind == "held" else "failure"
    assert report["retried"] == [item.request_id]
    assert report["unresolved"] == {DAY.isoformat(): [reason]}
    assert len(store.put_keys) == writes

    # A new successful receipt resolves the scope. Replaying the old failed
    # directory must not keep an obsolete failure alive forever.
    fresh_at = AT + timedelta(hours=1)
    fresh = FinancialAttempt(
        "fresh",
        DAY,
        fresh_at,
        tuple(Client(fresh_at).pages("financial_summaries", {"date": DAY.isoformat()})),
    )
    owner.commit([fresh])
    assert acquisition.main(args) == 0
    assert json.loads(capsys.readouterr().out)["unresolved"] == {}
    owner.commit(
        [
            FinancialAttempt(
                "new-failure", DAY, fresh_at + timedelta(hours=1), (), "provider_failure"
            )
        ]
    )
    assert acquisition.main(args) == 1
    assert json.loads(capsys.readouterr().out)["unresolved"] == {DAY.isoformat(): ["failure"]}


def test_commit_work_reports_current_scope_not_historical_failures(tmp_path, monkeypatch, capsys):
    from baibai_engine.data_collection.providers.jquants.financial_native import FinancialAttempt
    from baibai_jobs.acquisition import jquants_financial_native as acquisition

    class Store(MemoryR2Store):
        def close(self):
            pass

    store = Store()
    owner = FinancialL1(store, tmp_path / "work")
    owner.initialize()
    monkeypatch.setattr(acquisition, "Boto3R2Store", lambda **_: store)
    failed = FinancialAttempt("failed", DAY, AT, (), "provider_failure")
    later = AT + timedelta(hours=1)
    recovered = FinancialAttempt(
        "recovered",
        DAY,
        later,
        tuple(Client(later).pages("financial_summaries", {"date": DAY.isoformat()})),
    )
    for item in (failed, recovered):
        (owner.directory / f"attempt-{item.request_id}.json").write_text(json.dumps(item.value()))
    # Unrelated failure must not poison this directory's resolved source day.
    other_day = DAY + timedelta(days=1)
    owner.commit([FinancialAttempt("unrelated", other_day, AT, (), "provider_failure")])
    assert acquisition.main(["commit-work", "--directory", str(owner.directory)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["failed"] == ["failed"]
    assert report["changed"] == ["recovered"]
    assert report["unresolved"] == {}
