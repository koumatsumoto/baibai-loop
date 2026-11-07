"""通常の財務取得はnative原本だけを使い、旧取得planへ戻らない。"""

from datetime import date

import pytest

import baibai_jobs.integration.source_handoff as handoff


@pytest.mark.parametrize("old_flag", [None, "false", "true", "invalid"])
def test_native_ordinary_acquisition_never_resolves_legacy_release_or_creates_stage(
    tmp_path, monkeypatch, old_flag
):
    from tests.helpers.r2_store import MemoryR2Store

    import baibai_engine.data_collection.providers.jquants.http as http
    import baibai_engine.storage.snapshots.objects as objects
    import baibai_jobs.acquisition.jquants_financial_native as acquisition

    if old_flag is None:
        monkeypatch.delenv("BAIBAI_FINANCIAL_NATIVE", raising=False)
    else:
        monkeypatch.setenv("BAIBAI_FINANCIAL_NATIVE", old_flag)
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    calls = []
    store = MemoryR2Store()
    store.close = lambda: None
    client = type("Client", (), {"close": lambda _: None})()
    monkeypatch.setattr(handoff, "Boto3R2Store", lambda **kw: store)
    monkeypatch.setattr(http, "JQuantsHTTP", lambda *a: client)
    monkeypatch.setattr(
        objects, "open_lake", lambda **kw: pytest.fail("legacy release was resolved")
    )
    monkeypatch.setattr(
        handoff, "save_handoff", lambda *a, **kw: pytest.fail("native L1 became a stage copy")
    )

    def acquire(owner, **kwargs):
        calls.append((owner, kwargs))
        return {"changed": 1, "failed": 0, "held": 0}

    monkeypatch.setattr(acquisition, "acquire_financial_native", acquire)
    assert (
        handoff.main(
            [
                "acquire",
                "--source",
                "jquants-financial",
                "--asof",
                "2026-11-02",
                "--directory",
                str(tmp_path / "work"),
            ]
        )
        == 0
    )
    assert calls[0][0].store is store
    assert calls[0][1]["end"] == date(2026, 11, 2)
    assert output.read_text().splitlines() == ["stage_ready=false", "native_l1=true"]
    assert not (tmp_path / "work" / "receipts.sqlite").exists()


@pytest.mark.parametrize("failure_point", ["pack", "head"])
def test_new_runner_resumes_only_durable_head_after_failed_native_publication(
    tmp_path, monkeypatch, failure_point
):
    from datetime import UTC, datetime

    from tests.helpers.r2_store import MemoryR2Store

    import baibai_engine.data_collection.providers.jquants.http as http
    import baibai_engine.primitives.market_windows as windows
    from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
    from baibai_engine.data_collection.providers.jquants.financial_native import (
        HEAD_KEY,
        PREFIX,
        FinancialL1,
    )
    from baibai_engine.storage.objects.r2 import R2Error

    class Store(MemoryR2Store):
        fail = False

        def close(self):
            pass

        def put_file(self, key, path, **kwargs):
            target = (
                key.startswith(PREFIX + "packs/") if failure_point == "pack" else key == HEAD_KEY
            )
            if self.fail and target:
                raise R2Error("synthetic write failure")
            return super().put_file(key, path, **kwargs)

    calls = []

    class Client:
        def close(self):
            pass

        def pages(self, source, params):
            calls.append(params)
            yield JQuantsPage(source, params, datetime(2026, 11, 3, tzinfo=UTC), b'{"data":[]}')

    store = Store()
    initial = FinancialL1(store, tmp_path / "init").initialize()
    monkeypatch.setattr(windows, "FINANCIAL_HISTORY_DAYS", 0)
    monkeypatch.setattr(windows, "FINANCIAL_REVISION_OVERLAP_DAYS", 0)
    monkeypatch.setattr(handoff, "Boto3R2Store", lambda **kw: store)
    monkeypatch.setattr(http, "JQuantsHTTP", lambda *a: Client())
    arguments = ["acquire", "--source", "jquants-financial", "--asof", "2026-11-02", "--directory"]
    store.fail = True
    assert handoff.main([*arguments, str(tmp_path / "runner-1")]) == 1
    assert FinancialL1(store, tmp_path / "inspect").fixed().payload == initial.payload
    store.fail = False
    assert handoff.main([*arguments, str(tmp_path / "runner-2")]) == 0
    assert len(calls) == 2
    fixed = FinancialL1(store, tmp_path / "runner-3").resume()
    assert len(fixed.value()["receipts"]) == 1
    assert next(fixed.requests(store, days=[date(2026, 11, 2)])).request.records() == []


@pytest.mark.parametrize("old_mode", [False, None, "false", 1])
def test_old_financial_plan_values_never_rebind_to_native_input(tmp_path, old_mode):
    import json

    from baibai_jobs.integration.sources import apply

    (tmp_path / "plan.json").write_text(json.dumps({"financial_native": old_mode, "handoffs": []}))
    with pytest.raises(ValueError, match="legacy financial mode"):
        apply(object(), tmp_path, market=tmp_path / "not-opened", asof=date(2026, 11, 2))
    assert not (tmp_path / "not-opened").exists()


def test_new_plan_omits_financial_mode_and_old_financial_stages(tmp_path, monkeypatch):
    import json

    from tests.helpers.edinet_native import empty_edinet_input
    from tests.helpers.r2_store import MemoryR2Store

    import baibai_jobs.integration.sources as integration

    store = MemoryR2Store()
    empty_edinet_input(store, tmp_path / "native")
    monkeypatch.setenv("BAIBAI_FINANCIAL_NATIVE", "false")
    monkeypatch.setattr(integration, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(integration, "available_ready", lambda _: ([], []))
    monkeypatch.setattr(integration, "pending_handoffs", lambda _: [])
    monkeypatch.setattr(
        integration,
        "available_handoffs",
        lambda *_, **__: [{"source": "jquants-financial"}, {"source": "jpx-earnings"}],
    )
    directory = tmp_path / "plan"
    assert integration.main(["plan", "--directory", str(directory)]) == 0
    plan = json.loads((directory / "plan.json").read_text())
    assert "financial_native" not in plan
    assert plan["handoffs"] == [{"source": "jpx-earnings"}]
    assert plan["edinet_input"]["kind"] == "native_l1"


@pytest.mark.parametrize("command", ["input", "resume", "export", "stage", "replay-copy"])
def test_financial_stage_commands_are_rejected_before_store_or_sqlite_access(
    tmp_path, monkeypatch, command
):
    monkeypatch.setattr(
        handoff, "Boto3R2Store", lambda **_: pytest.fail("retired financial stage accessed store")
    )
    assert (
        handoff.main(
            [command, "--source", "jquants-financial", "--directory", str(tmp_path / "unopened")]
        )
        == 1
    )
    assert not (tmp_path / "unopened").exists()
