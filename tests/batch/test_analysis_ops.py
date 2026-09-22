from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from tests.helpers.macro_context import macro_context_payload
from tests.helpers.research_triage import published_review_set, research_triage_payload
from tests.helpers.screening_run import screening_run_payload, security_analysis

import baibai_batch.analysis.cli as analysis_cli
from baibai_batch.analysis.cli import PipelineLock, RunLog
from baibai_batch.analysis.io import ensure_private_dir, read_json
from baibai_batch.analysis.models import ModelOutput, ModelUsage
from baibai_engine.appdb.write import initialize_database
from baibai_engine.batch_api import DailyAnalysisContext
from baibai_engine.foundation.research_triage import ResearchTriage
from baibai_engine.macro.context.models import MacroContextDocument
from baibai_engine.read_api import research_triage_payloads_for_review_set
from baibai_engine.screening.discovery.review_set import PublishedReviewSet, build_review_set
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.rules_identity import production_rules_contract_hash
from baibai_engine.screening.run_store import ScreeningRunReader, ScreeningRunStore

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


def _context(
    *,
    tickers: tuple[str, ...] = ("2331", "0001"),
    existing: ResearchTriage | None = None,
    macro_context: MacroContextDocument | None = None,
) -> DailyAnalysisContext:
    return DailyAnalysisContext(
        review_set=_review_set(tickers=tickers),
        existing_triage=existing,
        macro_context=macro_context,
    )


def _args(tmp_path: Path, *, asof: date | None = _ASOF) -> SimpleNamespace:
    return SimpleNamespace(
        asof=asof,
        latest=False,
        state_dir=tmp_path / "state",
        repo_root=tmp_path,
        format="json",
    )


def _macro_context() -> MacroContextDocument:
    return MacroContextDocument.model_validate(
        macro_context_payload(
            context_id="macro-context-2026-09-01-analysis",
            as_of="2026-09-01",
            published_at="2026-09-01T10:00:00+09:00",
        )
    )


def _model_runner(verdicts: dict[str, str]):
    calls: list[object] = []

    def run(model_input, run_dir, state_root, log):
        del run_dir, state_root, log
        calls.append(model_input)
        decisions = []
        priority = 0
        for candidate in model_input.candidates:
            research = verdicts[candidate.ticker] == "research"
            if research:
                priority += 1
            decisions.append(
                {
                    "ticker": candidate.ticker,
                    "verdict": "research" if research else "skip",
                    "priority": priority if research else None,
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


def _seed_canonical_review_set(
    root: Path,
    *,
    asof: date = _ASOF,
    suffix: str = "canonical",
    created_time: str = "18:30:00",
    run_suffix: str | None = None,
    run_at: str | None = None,
) -> PublishedReviewSet:
    run_suffix = run_suffix or suffix
    app_db = root / "stores/application/baibai.sqlite"
    runs_db = root / "stores/screening/runs.sqlite"
    initialize_database(app_db)
    screening_rules = load_screening_rules()
    rules = screening_rules.candidate_discovery
    required_jpx_flags = screening_rules.universe.required_jpx_flags
    rules_hash = production_rules_contract_hash(screening_rules.model_dump_json())
    analysis = security_analysis(
        ticker="2331",
        name="Company 2331",
        sector_33="情報・通信業",
        per_forward=10.0,
        per_trailing=11.0,
        pbr=0.8,
        p_s=1.0,
        ev_ebitda=5.0,
        pcfr=8.0,
        metrics={
            "per_forward_sector_gap": -0.5,
            "normalized_per_3fy": 10.0,
            "fcf_yield": 0.08,
            "ocf_yield": 0.1,
            "asset_backed_ratio": 0.5,
            "net_cash_to_market_cap": 0.25,
            "pbr_sector_gap": -0.3,
            "equity_ratio": 0.6,
            "p_s_sector_gap": -0.4,
            "sales_yoy": 0.05,
            "operating_profit": 12.0,
            "sales_ttm": 100.0,
            "total_assets": 200.0,
            "debt": 20.0,
            "cash": 30.0,
            "er_annual": 0.13,
        },
    )
    ScreeningRunStore(runs_db).publish_run(
        screening_run_payload(
            as_of=asof.isoformat(),
            run_at=run_at,
            rules_hash=rules_hash,
            security_analyses=(analysis,),
        ),
        run_revision_id=f"run-cloud-{run_suffix}",
    )
    payload = {
        **build_review_set([analysis], rules=rules, required_jpx_flags=required_jpx_flags),
        "review_set_id": f"review-set-cloud-{suffix}",
        "run_revision_id": f"run-cloud-{run_suffix}",
        "as_of": asof.isoformat(),
        "created_at": f"{asof.isoformat()}T{created_time}+09:00",
        "screening_rules_hash": rules_hash,
    }
    ScreeningRunStore(runs_db).publish_review_set(
        run_revision_id=f"run-cloud-{run_suffix}",
        payload=payload,
        rules=rules,
        required_jpx_flags=required_jpx_flags,
        review_set_id=f"review-set-cloud-{suffix}",
    )
    return PublishedReviewSet.model_validate(payload)


def test_default_asof_without_exact_review_set_launches_no_model_or_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved_asofs: list[date] = []

    def load_context(asof: date, **_kwargs: object) -> None:
        resolved_asofs.append(asof)

    monkeypatch.setattr(analysis_cli, "load_daily_analysis_context", load_context)
    model, calls = _model_runner({})

    assert analysis_cli._run(_args(tmp_path, asof=None), model_runner=model) == 0

    summary = _summary(tmp_path)
    assert summary["status"] == "no_review_set"
    assert summary["model_process_launches"] == 0
    assert summary["machine_commands"] == 0
    assert resolved_asofs == [date.fromisoformat(str(summary["as_of"]))]
    assert calls == []
    run_dir = Path(str(summary["run_dir"]))
    assert {path.name for path in run_dir.iterdir()} == {"run.log", "summary.json"}


def test_empty_review_set_writes_no_triage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = _context(tickers=())
    publish_calls: list[object] = []
    monkeypatch.setattr(
        analysis_cli, "load_daily_analysis_context", lambda _asof, **_kwargs: context
    )
    monkeypatch.setattr(
        analysis_cli,
        "publish_daily_research_triage",
        lambda *args, **kwargs: publish_calls.append((args, kwargs)),
    )
    model, calls = _model_runner({})

    assert analysis_cli._run(_args(tmp_path), model_runner=model) == 0
    summary = _summary(tmp_path)
    assert summary["status"] == "empty_review_set"
    assert summary["machine_commands"] == 0
    assert calls == publish_calls == []


def test_oversized_input_launches_no_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model, calls = _model_runner({})
    monkeypatch.setattr(
        analysis_cli, "load_daily_analysis_context", lambda _asof, **_kwargs: _context()
    )
    monkeypatch.setattr(analysis_cli, "_MAX_MODEL_INPUT_BYTES", 1)
    assert analysis_cli._run(_args(tmp_path), model_runner=model) == 1
    assert _summary(tmp_path)["model_process_launches"] == 0
    assert calls == []


def test_analysis_reads_canonical_review_set_without_writing_machine_store(
    tmp_path: Path,
) -> None:
    review_set = _seed_canonical_review_set(tmp_path)
    runs_db = tmp_path / "stores/screening/runs.sqlite"
    reader = ScreeningRunReader(runs_db)
    before = (len(reader.list_runs()), len(reader.list_review_sets()))
    model, calls = _model_runner({"2331": "skip"})

    assert analysis_cli._run(_args(tmp_path), model_runner=model) == 0

    summary = _summary(tmp_path)
    assert summary["status"] == "published_all_skip"
    assert summary["machine_commands"] == 0
    assert len(calls) == 1
    assert (len(reader.list_runs()), len(reader.list_review_sets())) == before
    triages = research_triage_payloads_for_review_set(
        tmp_path / "stores/application/baibai.sqlite", review_set.review_set_id
    )
    assert len(triages) == 1
    assert triages[0]["review_set_id"] == review_set.review_set_id
    assert triages[0]["run_revision_id"] == review_set.run_revision_id


@pytest.mark.parametrize(
    ("verdicts", "expected_status", "awaits_human"),
    [
        ({"2331": "skip", "0001": "skip"}, "published_all_skip", False),
        ({"2331": "research", "0001": "skip"}, "published_awaiting_human", True),
    ],
)
def test_one_model_request_publishes_without_starting_an_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verdicts: dict[str, str],
    expected_status: str,
    awaits_human: bool,
) -> None:
    context = _context(macro_context=_macro_context())
    loaded: list[dict[str, object]] = []

    def load(asof, **paths):
        assert asof == _ASOF
        loaded.append(paths)
        return context

    monkeypatch.setattr(analysis_cli, "load_daily_analysis_context", load)
    published: list[ResearchTriage] = []

    def publish(
        _review_set_id,
        decisions,
        *,
        macro_context_id,
        app_db_path,
        runs_db_path,
        published_at,
    ):
        assert app_db_path == tmp_path / "stores/application/baibai.sqlite"
        assert runs_db_path == tmp_path / "stores/screening/runs.sqlite"
        assert context.macro_context is not None
        assert macro_context_id == context.macro_context.context_id
        del published_at
        selected = tuple(item.ticker for item in decisions if item.verdict == "research")
        triage = _triage(context.review_set, research=selected)
        published.append(triage)
        return triage

    monkeypatch.setattr(analysis_cli, "publish_daily_research_triage", publish)
    model, calls = _model_runner(verdicts)

    assert analysis_cli._run(_args(tmp_path), model_runner=model) == 0

    summary = _summary(tmp_path)
    assert summary["status"] == expected_status
    assert summary["model_process_launches"] == 1
    assert summary["model_requests"] == 1
    assert summary["actual_input_tokens"] == 120
    assert summary["ai_file_reads"] == summary["ai_tool_calls"] == 0
    assert summary["machine_commands"] == 0
    assert len(calls) == len(published) == 1
    assert loaded == [
        {
            "app_db_path": tmp_path / "stores/application/baibai.sqlite",
            "runs_db_path": tmp_path / "stores/screening/runs.sqlite",
        }
    ]
    assert (summary["human_action"] is not None) is awaits_human
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


def test_existing_exact_triage_launches_no_model_and_stays_awaiting_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review_set = _review_set()
    triage = _triage(review_set)
    context = DailyAnalysisContext(review_set, triage, None)
    monkeypatch.setattr(
        analysis_cli, "load_daily_analysis_context", lambda _asof, **_kwargs: context
    )
    publish_calls: list[object] = []
    monkeypatch.setattr(
        analysis_cli,
        "publish_daily_research_triage",
        lambda *args, **kwargs: publish_calls.append((args, kwargs)),
    )
    model, calls = _model_runner({})

    assert analysis_cli._run(_args(tmp_path), model_runner=model) == 0
    summary = _summary(tmp_path)
    assert summary["status"] == "awaiting_human"
    assert summary["model_process_launches"] == 0
    assert calls == publish_calls == []


@pytest.mark.parametrize("failure", ["adapter", "publish"])
def test_failure_before_or_at_canonical_boundary_writes_no_triage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    context = _context()
    monkeypatch.setattr(
        analysis_cli, "load_daily_analysis_context", lambda _asof, **_kwargs: context
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


def test_adapter_failure_can_be_retried_as_a_fresh_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context()
    monkeypatch.setattr(
        analysis_cli, "load_daily_analysis_context", lambda _asof, **_kwargs: context
    )
    triage = _triage(context.review_set, research=())
    monkeypatch.setattr(
        analysis_cli, "publish_daily_research_triage", lambda *_args, **_kwargs: triage
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
        "priority",
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
                        "priority": None,
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
                        "priority": None,
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
        'priority': None,
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
    context = _context()
    model_input = analysis_cli.build_model_input(context.review_set, context.macro_context)
    log = RunLog(run_dir / "run.log", state_root=state)

    output, usage, input_bytes = analysis_cli._run_model(model_input, run_dir, state, log)

    assert len(output.decisions) == 2
    assert usage.model_requests == 1
    assert usage.input_tokens == 321
    assert usage.output_tokens == 54
    assert usage.tool_calls == 0
    assert input_bytes == analysis_cli._model_input_bytes(model_input)
    assert not (run_dir / ".output-schema.json").exists()
    assert {path.name for path in run_dir.iterdir()} == {"run.log", "result.json"}


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("output_format", ["text", "json"])
def test_success_hands_every_canonical_candidate_to_the_human(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    existing: bool,
    output_format: str,
) -> None:
    review_set = _review_set(tickers=("2331", "0001", "0002"))
    triage = _triage(review_set, research=("2331", "0001"))
    # Serialized ticker order deliberately differs from judgment priority.
    triage = triage.model_copy(update={"entries": tuple(reversed(triage.entries))})
    monkeypatch.setattr(
        analysis_cli,
        "load_daily_analysis_context",
        lambda _asof, **_kwargs: DailyAnalysisContext(
            review_set, triage if existing else None, None
        ),
    )
    published = []

    def publish(*_args, **_kwargs):
        published.append(triage)
        return triage

    monkeypatch.setattr(analysis_cli, "publish_daily_research_triage", publish)
    model, calls = _model_runner({"2331": "research", "0001": "research", "0002": "skip"})
    args = _args(tmp_path)
    args.format = output_format
    assert analysis_cli._run(args, model_runner=model) == 0
    stdout = capsys.readouterr().out
    summary = _summary(tmp_path)
    assert summary["research_triage_id"] == triage.research_triage_id
    assert summary["as_of"] == "2026-09-01"
    expected = [
        {
            "ticker": entry.ticker,
            "priority": entry.priority,
            "rationale": entry.rationale,
            "research_question": entry.research_question,
            "key_risk": entry.key_risk,
        }
        for entry in sorted(triage.entries, key=lambda entry: entry.priority or 0)
        if entry.decision == "research"
    ]
    assert summary["research_candidates"] == expected
    assert [entry["priority"] for entry in expected] == [1, 2]
    assert len(calls) == len(published) == (0 if existing else 1)
    if output_format == "json":
        assert json.loads(stdout)["research_candidates"] == expected
    else:
        assert f"research_triage_id={triage.research_triage_id}" in stdout
        assert stdout.index("priority=1 ticker=") < stdout.index("priority=2 ticker=")
        for entry in expected:
            for field in ("ticker", "rationale", "research_question", "key_risk"):
                assert entry[field] in stdout


def test_published_stdout_id_prepares_the_exact_research_set(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tests.helpers.db_seed import seed_ledger
    from tests.helpers.ledger import load_portfolio_ledger

    from baibai_engine.operation.service import OperationService
    from baibai_engine.research.workspace_cli import main as research_main

    _seed_canonical_review_set(tmp_path)
    app_db = tmp_path / "stores/application/baibai.sqlite"
    seed_ledger(
        app_db,
        load_portfolio_ledger(
            Path(__file__).resolve().parents[1] / "fixtures/portfolio-ledger/representative.yaml"
        ).model_copy(update={"market_prices": ()}),
    )
    model, calls = _model_runner({"2331": "research"})
    assert analysis_cli._run(_args(tmp_path), model_runner=model) == 0
    first = json.loads(capsys.readouterr().out)
    assert analysis_cli._run(_args(tmp_path), model_runner=model) == 0
    second = json.loads(capsys.readouterr().out)
    assert first["research_triage_id"] == second["research_triage_id"]
    assert first["research_candidates"] == second["research_candidates"]
    assert len(calls) == 1
    assert (
        research_main(
            [
                "prepare",
                "--research-triage-id",
                second["research_triage_id"],
                "--ticker",
                "2331",
                "--db",
                str(app_db),
                "--workspace",
                str(tmp_path / "workspace"),
            ]
        )
        == 0
    )
    operation = OperationService(app_db).active()
    assert operation is not None
    assert operation.payload.canonical_refs == (first["research_triage_id"],)
    assert operation.payload.artifacts[0]["research_set"] == ["2331"]


def test_analysis_selector_parser() -> None:
    parser = analysis_cli.build_parser()
    assert parser.parse_args(["run", "--latest"]).latest is True
    default = parser.parse_args(["run"])
    assert default.latest is False
    assert default.asof is None
    with pytest.raises(SystemExit) as error:
        parser.parse_args(["run", "--latest", "--asof", "2026-09-18"])
    assert error.value.code == 2


def test_weekend_latest_publishes_and_reuses_exact_canonical_triage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Sunday(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 20, 12, tzinfo=tz)

    monkeypatch.setattr(analysis_cli, "datetime", Sunday)
    _seed_canonical_review_set(tmp_path, asof=date(2026, 9, 17), suffix="older")
    _seed_canonical_review_set(tmp_path, asof=date(2026, 9, 18))
    _seed_canonical_review_set(
        tmp_path,
        asof=date(2026, 9, 18),
        suffix="zz",
        run_suffix="canonical",
        created_time="18:15:00",
    )
    # Same timestamp exercises the resolver's review_set_id tie-break.
    selected = _seed_canonical_review_set(
        tmp_path, asof=date(2026, 9, 18), suffix="z", run_suffix="canonical"
    )
    model, calls = _model_runner({"2331": "research"})
    args = _args(tmp_path, asof=None)
    assert analysis_cli._run(args, model_runner=model) == 0
    default = json.loads(capsys.readouterr().out)
    assert default["status"] == "no_review_set"
    assert default["as_of"] == "2026-09-20"
    assert calls == []
    args.latest = True
    assert analysis_cli._run(args, model_runner=model) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["as_of"] == "2026-09-18"
    assert first["status"] == "published_awaiting_human"
    payloads = research_triage_payloads_for_review_set(
        tmp_path / "stores/application/baibai.sqlite", selected.review_set_id
    )
    assert len(payloads) == 1
    assert payloads[0]["research_triage_id"] == first["research_triage_id"]
    assert payloads[0]["run_revision_id"] == selected.run_revision_id
    assert analysis_cli._run(args, model_runner=model) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["research_triage_id"] == first["research_triage_id"]
    assert repeated["status"] == "awaiting_human"
    assert repeated["model_process_launches"] == 0
    assert len(calls) == 1


@pytest.mark.parametrize(
    "failure", ["empty", "new_day", "new_revision", "mixed_offset", "binding", "payload"]
)
def test_latest_failure_launches_no_model_and_writes_no_triage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    review_set = _seed_canonical_review_set(tmp_path)
    runs_db = tmp_path / "stores/screening/runs.sqlite"
    if failure in {"new_day", "new_revision", "mixed_offset"}:
        ScreeningRunStore(runs_db).publish_run(
            screening_run_payload(
                as_of="2026-09-02" if failure == "new_day" else _ASOF.isoformat(),
                run_at=f"{_ASOF.isoformat()}T10:00:00+00:00"
                if failure == "mixed_offset"
                else "2026-09-02T18:00:00+09:00",
            ),
            run_revision_id="run-newer-incomplete",
        )
    else:
        with closing(sqlite3.connect(runs_db)) as connection, connection:
            if failure == "empty":
                connection.execute("DELETE FROM review_set")
            else:
                payload = review_set.model_dump(mode="json")
                if failure == "binding":
                    payload["review_set_id"] = "wrong-identity"
                else:
                    payload["entries"] = "invalid"
                connection.execute("UPDATE review_set SET payload = ?", (json.dumps(payload),))
    writes = []
    monkeypatch.setattr(
        analysis_cli, "publish_daily_research_triage", lambda *a, **kw: writes.append(a)
    )
    model, calls = _model_runner({})
    args = _args(tmp_path, asof=None)
    args.latest = True
    args.format = "text"
    assert analysis_cli._run(args, model_runner=model) == 1
    summary = _summary(tmp_path)
    assert summary["status"] == "failed"
    assert summary["failure"]
    assert f"failure={summary['failure']}" in capsys.readouterr().out
    assert summary["model_process_launches"] == 0
    assert calls == writes == []
    assert (
        research_triage_payloads_for_review_set(
            tmp_path / "stores/application/baibai.sqlite", review_set.review_set_id
        )
        == []
    )


def test_latest_matches_run_and_review_set_by_instant_with_mixed_offsets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_canonical_review_set(tmp_path, suffix="old-offset", created_time="18:15:00")
    selected = _seed_canonical_review_set(
        tmp_path,
        suffix="new-offset",
        run_at=f"{_ASOF.isoformat()}T10:00:00+00:00",
        created_time="19:30:00",
    )
    model, calls = _model_runner({"2331": "research"})
    args = _args(tmp_path, asof=None)
    args.latest = True
    assert analysis_cli._run(args, model_runner=model) == 0
    result = json.loads(capsys.readouterr().out)
    payloads = research_triage_payloads_for_review_set(
        tmp_path / "stores/application/baibai.sqlite", selected.review_set_id
    )
    assert len(payloads) == len(calls) == 1
    assert payloads[0]["research_triage_id"] == result["research_triage_id"]
    assert payloads[0]["run_revision_id"] == selected.run_revision_id
