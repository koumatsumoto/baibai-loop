"""The shared rollout selection switches ordinary acquisition and daily together."""

from datetime import date

import pytest

import baibai_jobs.integration.source_handoff as handoff
from baibai_jobs.financial_cutover import financial_native_enabled


def test_native_ordinary_acquisition_never_resolves_legacy_release_or_creates_stage(
    tmp_path, monkeypatch
):
    from tests.helpers.r2_store import MemoryR2Store

    import baibai_engine.data_collection.providers.jquants.http as http
    import baibai_engine.storage.snapshots.objects as objects
    import baibai_jobs.acquisition.jquants_financial_native as acquisition

    monkeypatch.setenv("BAIBAI_FINANCIAL_NATIVE", "true")
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


def test_native_integration_rejects_a_plan_from_the_old_writer_mode(tmp_path, monkeypatch):
    from baibai_jobs.integration.sources import apply

    (tmp_path / "plan.json").write_text('{"financial_native": false, "handoffs": []}')
    monkeypatch.setenv("BAIBAI_FINANCIAL_NATIVE", "true")
    with pytest.raises(ValueError, match="fixed integration plan"):
        apply(
            object(),
            tmp_path,
            market=tmp_path / "not-opened",
            economic=tmp_path / "not-opened-either",
            asof=date(2026, 11, 2),
        )


def test_invalid_rollout_value_does_not_silently_choose_a_writer(monkeypatch):
    monkeypatch.setenv("BAIBAI_FINANCIAL_NATIVE", "TRUE")
    with pytest.raises(ValueError, match="must be true or false"):
        financial_native_enabled()
