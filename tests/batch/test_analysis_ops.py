from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from tests.helpers.research_triage import published_review_set, research_triage_payload

import baibai_batch.analysis.cli as analysis_cli
from baibai_batch.analysis.cli import PipelineLock, RunLog
from baibai_batch.analysis.io import ensure_private_dir, read_json
from baibai_batch.analysis.models import ModelOutput, ModelUsage
from baibai_batch.jobs.daily import DailyBatchResult
from baibai_engine.batch_api import DailyAnalysisContext
from baibai_engine.foundation.research_triage import ResearchTriage
from baibai_engine.operation.models import OperationPayload, OperationSession
from baibai_engine.screening.discovery.review_set import PublishedReviewSet

_JST = ZoneInfo("Asia/Tokyo")
_ASOF = date(2026, 9, 1)


def _review_set(*, tickers: tuple[str, ...] = ("2331", "0001")) -> PublishedReviewSet:
    return PublishedReviewSet.model_validate(
        published_review_set(
            as_of=_ASOF.isoformat(),
            review_set_id="review-set-daily",
            run_revision_id="run-daily",
            tickers=tickers,
        )
    )


def _triage(
    review_set: PublishedReviewSet, *, research: tuple[str, ...] = ("2331",)
) -> ResearchTriage:
    priority = 0
    entries: list[dict[str, object]] = []
    for source in review_set.entries:
        selected = source.ticker in research
        if selected:
            priority += 1
        entries.append(
            {
                "ticker": source.ticker,
                "decision": "research" if selected else "skip",
                "priority": priority if selected else None,
                "rationale": "一次開示で収益持続性を確認する"
                if selected
                else "追加調査で識別する仮説がない",
                "research_question": "粗利は持続するか" if selected else None,
                "key_risk": "顧客集中" if selected else None,
                "candidate_snapshot": {
                    "name": source.name,
                    "sector_33": source.sector_33,
                    "review_position": source.review_position,
                    "nominations": [item.model_dump(mode="json") for item in source.nominations],
                    "analysis": source.analysis.model_dump(mode="json"),
                },
            }
        )
    return ResearchTriage.model_validate(
        research_triage_payload(
            research_triage_id="research-triage-daily",
            review_set_id=review_set.review_set_id,
            run_revision_id=review_set.run_revision_id,
            as_of=review_set.as_of.isoformat(),
            published_at="2026-09-01T18:00:00+09:00",
            entries=entries,
        )
    )


def _operation(triage: ResearchTriage) -> OperationSession:
    return OperationSession(
        operation_id="op-20260901-capital-allocation-1",
        session_kind="capital-allocation",
        status="active",
        as_of=triage.as_of,
        started_at=datetime(2026, 9, 1, 18, 1, tzinfo=_JST),
        payload=OperationPayload(
            checkpoint="awaiting Research Set",
            artifacts=(
                {
                    "kind": "research_triage",
                    "ref": triage.research_triage_id,
                    "research_count": len(triage.researchable_tickers()),
                },
            ),
            canonical_refs=(triage.research_triage_id,),
            human_confirmation={"request": "confirm Research Set"},
            next="wait",
        ),
    )


def _context(
    *,
    tickers: tuple[str, ...] = ("2331", "0001"),
    existing: ResearchTriage | None = None,
    active: OperationSession | None = None,
) -> DailyAnalysisContext:
    return DailyAnalysisContext(
        review_set=_review_set(tickers=tickers),
        existing_triage=existing,
        macro_context=None,
        active_operation=active,
    )


def _args(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        asof=_ASOF,
        state_dir=tmp_path / "state",
        repo_root=tmp_path,
        format="json",
    )


def _daily(*, status: str = "machine_complete") -> DailyBatchResult:
    if status == "skipped_non_business_day":
        return DailyBatchResult(status, _ASOF.isoformat(), 0)
    return DailyBatchResult(
        status,
        _ASOF.isoformat(),
        0,
        run_revision_id="run-daily",
        review_set_id="review-set-daily",
    )


def _model_runner(verdicts: dict[str, str]):
    calls: list[object] = []

    def run(model_input, run_dir, state_root, log):
        del run_dir, state_root, log
        calls.append(model_input)
        decisions = []
        for candidate in model_input.candidates:
            research = verdicts[candidate.ticker] == "research"
            decisions.append(
                {
                    "ticker": candidate.ticker,
                    "verdict": "research" if research else "skip",
                    "rationale": "一次開示で収益持続性を確認する"
                    if research
                    else "追加調査で識別する仮説がない",
                    "research_question": "粗利は持続するか" if research else None,
                    "key_risk": "顧客集中" if research else None,
                }
            )
        return (
            ModelOutput.model_validate({"schema_version": 1, "decisions": decisions}),
            ModelUsage(
                model_requests=1,
                input_tokens=120,
                output_tokens=40,
                duration_seconds=0.5,
                tool_calls=0,
            ),
            1234,
        )

    return run, calls


def _summary(tmp_path: Path) -> dict[str, object]:
    summaries = list((tmp_path / "state/analysis").glob("*/summary.json"))
    assert len(summaries) == 1
    value = json.loads(summaries[0].read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_non_business_day_launches_no_model_and_writes_four_or_fewer_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        analysis_cli,
        "run_daily_batch_structured",
        lambda **_kwargs: _daily(status="skipped_non_business_day"),
    )
    model, calls = _model_runner({})

    assert analysis_cli._run(_args(tmp_path), model_runner=model) == 0

    summary = _summary(tmp_path)
    assert summary["status"] == "skipped_non_business_day"
    assert summary["model_process_launches"] == 0
    assert calls == []
    run_dir = Path(str(summary["run_dir"]))
    assert {path.name for path in run_dir.iterdir()} == {"run.log", "summary.json"}


def test_empty_review_set_writes_no_triage_or_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(tickers=())
    publish_calls: list[object] = []
    operation_calls: list[object] = []
    monkeypatch.setattr(analysis_cli, "run_daily_batch_structured", lambda **_kwargs: _daily())
    monkeypatch.setattr(analysis_cli, "load_daily_analysis_context", lambda _id: context)
    monkeypatch.setattr(
        analysis_cli,
        "publish_daily_research_triage",
        lambda *args, **kwargs: publish_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        analysis_cli,
        "ensure_daily_research_operation",
        lambda *args, **kwargs: operation_calls.append((args, kwargs)),
    )
    model, calls = _model_runner({})

    assert analysis_cli._run(_args(tmp_path), model_runner=model) == 0
    assert _summary(tmp_path)["status"] == "empty_review_set"
    assert calls == publish_calls == operation_calls == []


def test_missing_review_set_or_oversized_input_launches_no_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        analysis_cli,
        "run_daily_batch_structured",
        lambda **_kwargs: DailyBatchResult("machine_complete", _ASOF.isoformat(), 0),
    )
    model, calls = _model_runner({})
    assert analysis_cli._run(_args(tmp_path), model_runner=model) == 1
    assert _summary(tmp_path)["model_process_launches"] == 0
    assert calls == []

    second_root = tmp_path / "second"
    monkeypatch.setattr(analysis_cli, "run_daily_batch_structured", lambda **_kwargs: _daily())
    monkeypatch.setattr(analysis_cli, "load_daily_analysis_context", lambda _id: _context())
    monkeypatch.setattr(analysis_cli, "_MAX_MODEL_INPUT_BYTES", 1)
    assert analysis_cli._run(_args(second_root), model_runner=model) == 1
    assert _summary(second_root)["model_process_launches"] == 0
    assert calls == []


def test_active_operation_stops_before_model_without_adopting_same_asof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review_set = _review_set()
    unrelated_triage = _triage(review_set)
    context = _context(active=_operation(unrelated_triage))
    monkeypatch.setattr(analysis_cli, "run_daily_batch_structured", lambda **_kwargs: _daily())
    monkeypatch.setattr(analysis_cli, "load_daily_analysis_context", lambda _id: context)
    model, calls = _model_runner({})

    assert analysis_cli._run(_args(tmp_path), model_runner=model) == 0
    assert _summary(tmp_path)["status"] == "blocked_by_active_operation"
    assert calls == []


@pytest.mark.parametrize(
    ("verdicts", "expected_status", "operation_exists"),
    [
        ({"2331": "skip", "0001": "skip"}, "published_all_skip", False),
        ({"2331": "research", "0001": "skip"}, "published_awaiting_human", True),
    ],
)
def test_one_model_request_publishes_and_only_research_starts_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verdicts: dict[str, str],
    expected_status: str,
    operation_exists: bool,
) -> None:
    context = _context()
    monkeypatch.setattr(analysis_cli, "run_daily_batch_structured", lambda **_kwargs: _daily())
    monkeypatch.setattr(analysis_cli, "load_daily_analysis_context", lambda _id: context)
    published: list[ResearchTriage] = []

    def publish(_review_set_id, decisions, *, published_at):
        del published_at
        selected = tuple(item.ticker for item in decisions if item.verdict == "research")
        triage = _triage(context.review_set, research=selected)
        published.append(triage)
        return triage

    monkeypatch.setattr(analysis_cli, "publish_daily_research_triage", publish)
    operation_calls: list[ResearchTriage] = []

    def ensure(triage, *, started_at):
        del started_at
        operation_calls.append(triage)
        return _operation(triage) if triage.researchable_tickers() else None

    monkeypatch.setattr(analysis_cli, "ensure_daily_research_operation", ensure)
    model, calls = _model_runner(verdicts)

    assert analysis_cli._run(_args(tmp_path), model_runner=model) == 0

    summary = _summary(tmp_path)
    assert summary["status"] == expected_status
    assert summary["model_process_launches"] == 1
    assert summary["model_requests"] == 1
    assert summary["actual_input_tokens"] == 120
    assert summary["ai_file_reads"] == summary["ai_tool_calls"] == 0
    assert len(calls) == len(published) == len(operation_calls) == 1
    assert (summary["human_action"] is not None) is operation_exists
    input_payload = read_json(Path(str(summary["run_dir"])) / "input.json", root=tmp_path / "state")
    rendered = json.dumps(input_payload, ensure_ascii=False)
    assert rendered.count("macro_context") == 1
    assert all(
        forbidden not in rendered
        for forbidden in (
            "SKILL.md",
            "runbook",
            "review_set_id",
            "run_revision_id",
            "expected_prior",
            "publish",
            "workspace",
        )
    )


def test_existing_exact_triage_launches_no_model_and_reuses_only_exact_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review_set = _review_set()
    triage = _triage(review_set)
    context = DailyAnalysisContext(review_set, triage, None, _operation(triage))
    monkeypatch.setattr(analysis_cli, "run_daily_batch_structured", lambda **_kwargs: _daily())
    monkeypatch.setattr(analysis_cli, "load_daily_analysis_context", lambda _id: context)
    publish_calls: list[object] = []
    monkeypatch.setattr(
        analysis_cli,
        "publish_daily_research_triage",
        lambda *args, **kwargs: publish_calls.append((args, kwargs)),
    )
    ensure_calls: list[object] = []
    monkeypatch.setattr(
        analysis_cli,
        "ensure_daily_research_operation",
        lambda *args, **kwargs: ensure_calls.append((args, kwargs)) or context.active_operation,
    )
    model, calls = _model_runner({})

    assert analysis_cli._run(_args(tmp_path), model_runner=model) == 0
    summary = _summary(tmp_path)
    assert summary["status"] == "awaiting_human"
    assert summary["model_process_launches"] == 0
    assert calls == publish_calls == []
    assert len(ensure_calls) == 1


@pytest.mark.parametrize("failure", ["adapter", "publish"])
def test_failure_before_or_at_canonical_boundary_writes_no_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    context = _context()
    monkeypatch.setattr(analysis_cli, "run_daily_batch_structured", lambda **_kwargs: _daily())
    monkeypatch.setattr(analysis_cli, "load_daily_analysis_context", lambda _id: context)
    operation_calls: list[object] = []
    monkeypatch.setattr(
        analysis_cli,
        "ensure_daily_research_operation",
        lambda *args, **kwargs: operation_calls.append((args, kwargs)),
    )
    model, _calls = _model_runner({"2331": "research", "0001": "skip"})
    if failure == "adapter":

        def model(*_args):
            raise analysis_cli.ModelAdapterError("adapter unavailable")
    else:
        monkeypatch.setattr(
            analysis_cli,
            "publish_daily_research_triage",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ValueError("expected prior ID is stale")
            ),
        )

    assert analysis_cli._run(_args(tmp_path), model_runner=model) == 1
    assert _summary(tmp_path)["status"] == "failed"
    assert operation_calls == []


def test_adapter_failure_can_be_retried_as_a_fresh_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context()
    monkeypatch.setattr(analysis_cli, "run_daily_batch_structured", lambda **_kwargs: _daily())
    monkeypatch.setattr(analysis_cli, "load_daily_analysis_context", lambda _id: context)
    triage = _triage(context.review_set, research=())
    monkeypatch.setattr(
        analysis_cli, "publish_daily_research_triage", lambda *_args, **_kwargs: triage
    )
    monkeypatch.setattr(
        analysis_cli, "ensure_daily_research_operation", lambda *_args, **_kwargs: None
    )

    def fail(*_args):
        raise analysis_cli.ModelAdapterError("temporary adapter failure")

    good, _calls = _model_runner({"2331": "skip", "0001": "skip"})
    args = _args(tmp_path)
    assert analysis_cli._run(args, model_runner=fail) == 1
    assert analysis_cli._run(args, model_runner=good) == 0
    assert len(list((tmp_path / "state/analysis").glob("*/summary.json"))) == 2


def test_pipeline_lock_allows_only_one_local_execution(tmp_path: Path) -> None:
    state = ensure_private_dir(tmp_path / "state")
    first = PipelineLock(state)
    second = PipelineLock(state)
    with first, second:
        assert first.acquire() is True
        assert second.acquire() is False


def test_stdout_stays_compact_and_private_log_redacts_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = ensure_private_dir(tmp_path / "state")
    run_dir = ensure_private_dir(state / "analysis/run", root=state)
    log = RunLog(run_dir / "run.log", state_root=state)
    log.append("api_key=super-secret-value\ninternal detail")
    summary = analysis_cli._base_summary(_ASOF, run_dir, log)

    analysis_cli._emit(summary, "text")

    stdout = capsys.readouterr().out
    logged = log.path.read_text(encoding="utf-8")
    assert "super-secret-value" not in stdout
    assert "internal detail" not in stdout
    assert "super-secret-value" not in logged
    assert "[REDACTED]" in logged
    assert log.path.stat().st_mode & 0o777 == 0o600
    assert run_dir.stat().st_mode & 0o777 == 0o700


def test_model_result_is_strict_about_unknown_and_decision_dependent_fields() -> None:
    decision_schema = ModelOutput.model_json_schema()["$defs"]["DailyTriageDecision"]
    assert set(decision_schema["required"]) == {
        "ticker",
        "verdict",
        "rationale",
        "research_question",
        "key_risk",
    }
    with pytest.raises(ValidationError):
        ModelOutput.model_validate(
            {
                "schema_version": 1,
                "decisions": [
                    {
                        "ticker": "2331",
                        "verdict": "skip",
                        "rationale": "skip",
                    }
                ],
            }
        )
    with pytest.raises(ValidationError):
        ModelOutput.model_validate(
            {
                "schema_version": 1,
                "decisions": [
                    {
                        "ticker": "2331",
                        "verdict": "skip",
                        "rationale": "skip",
                        "research_question": None,
                        "key_risk": None,
                        "command": "publish",
                    }
                ],
            }
        )
    with pytest.raises(ValidationError):
        ModelOutput.model_validate(
            {
                "schema_version": 1,
                "decisions": [
                    {
                        "ticker": "2331",
                        "verdict": "research",
                        "rationale": "research",
                        "research_question": None,
                        "key_risk": None,
                    }
                ],
            }
        )


def test_codex_adapter_uses_one_stdin_payload_no_tools_and_records_actual_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = ensure_private_dir(tmp_path / "state")
    run_dir = ensure_private_dir(state / "analysis/run", root=state)
    fake = tmp_path / "codex"
    fake.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys

payload = json.load(sys.stdin)
result_path = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])
decisions = []
for candidate in payload['candidates']:
    decisions.append({
        'ticker': candidate['ticker'],
        'verdict': 'skip',
        'rationale': '追加調査で識別する仮説がない',
        'research_question': None,
        'key_risk': None,
    })
result_path.write_text(json.dumps({'schema_version': 1, 'decisions': decisions}))
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 321, 'output_tokens': 54}}))
""",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    monkeypatch.setattr(
        analysis_cli, "resolve_executable", lambda name: str(fake) if name == "codex" else name
    )
    model_input = analysis_cli._model_input(_context())
    log = RunLog(run_dir / "run.log", state_root=state)

    output, usage, input_bytes = analysis_cli._run_model(model_input, run_dir, state, log)

    assert len(output.decisions) == 2
    assert usage.model_requests == 1
    assert usage.input_tokens == 321
    assert usage.output_tokens == 54
    assert usage.tool_calls == 0
    assert input_bytes > len(json.dumps(model_input.model_dump(mode="json")).encode())
    assert not (run_dir / ".output-schema.json").exists()
    assert {path.name for path in run_dir.iterdir()} == {"run.log", "result.json"}
