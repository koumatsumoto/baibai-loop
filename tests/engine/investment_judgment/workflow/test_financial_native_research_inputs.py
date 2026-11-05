"""native財務から通常生成・保存Selection・CLI再開までの固定参照を確認する。"""

import hashlib
import json
import shutil
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta

import pyarrow.parquet as pq
import pytest
import yaml
from pydantic import ValidationError
from tests.helpers.lake_release import market_store
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.research_selection import selected_target

from baibai_engine.cli.operation import main as operation_main
from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.financial_native import (
    HEAD_KEY,
    FinancialAttempt,
    FinancialL1,
)
from baibai_engine.investment_judgment.workflow.generated_inputs import verify_generated_inputs
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionDraft,
    ResearchRequest,
)
from baibai_engine.investment_judgment.workflow.workspace.selected_prepare import (
    prepare_selected_workspace,
    resume_selected_workspace,
)
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.inputs import read_and_generate_models
from baibai_engine.machine_analysis.generation.query import read_research_model_inputs
from baibai_engine.machine_analysis.generation.store import (
    GenerationConflictError,
    ModelGenerationStore,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.snapshots.writer import sealed_sqlite_snapshot
from baibai_jobs.integration.financial_native import model_input, rebuild_saved_input

START = date(2026, 10, 30)
AS_OF = START + timedelta(days=3)
AT = datetime(2026, 11, 4, tzinfo=UTC)


def _attempt(identity, day, rows, *, at=AT):
    return FinancialAttempt(
        identity,
        day,
        at,
        (
            JQuantsPage(
                "financial_summaries",
                {"date": day.isoformat()},
                at,
                json.dumps({"data": rows}).encode(),
            ),
        ),
    )


def _selection_from_cli(app, operation_id, capsys):
    assert operation_main(["--db", str(app), "--format", "json", "show", operation_id]) == 0
    operation = json.loads(capsys.readouterr().out)
    return next(
        item for item in operation["payload"]["artifacts"] if item["kind"] == "research_selection"
    )


def _resolve(selection, pointer):
    value = selection
    for part in pointer.lstrip("/").split("/"):
        value = value[part.replace("~1", "/").replace("~0", "~")]
    return value


def test_production_native_generation_reference_survives_source_advance_and_cache_loss(
    tmp_path, monkeypatch, capsys
):
    store = MemoryR2Store()
    native = tmp_path / "native"
    owner = FinancialL1(store, native)
    owner.initialize()
    partial_day = START + timedelta(days=2)
    row = {"Code": "13010", "DiscDate": partial_day.isoformat(), "DiscNo": "1", "Sales": 100}
    owner.commit(
        [
            _attempt("empty", START, []),
            _attempt("partial", partial_day, [row, {"Code": "invalid"}]),
            _attempt("full", AS_OF, [{**row, "DiscDate": AS_OF.isoformat()}]),
        ]
    )
    owner.commit(
        [
            _attempt("held", START, [row]),
            FinancialAttempt("failed", AS_OF, AT + timedelta(hours=1), (), "timeout"),
        ]
    )
    fixed = owner.fixed()
    evaluated_at = AT + timedelta(hours=2)
    financial = model_input(store, fixed, as_of=AS_OF, evaluated_at=evaluated_at)
    expected_summary = financial.identity.descriptor["summary"]
    assert expected_summary["normal_empty_days"] == 0  # old empty receipt now has a held attempt
    assert expected_summary["unavailable_days"] == 1
    assert expected_summary["partial_days"] == 1
    assert expected_summary["failed_days"] == 1
    assert expected_summary["held_days"] == 1
    assert expected_summary["incomplete_days"] == 2

    market = market_store(
        tmp_path / "market",
        daily_bars=(("1301", AS_OF.isoformat(), 100.0, 1.0),),
        short_sale_reports=(),
    )
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=tmp_path / "mirror") as snapshot:
        generated = read_and_generate_models(
            generation_id="native-research-reference",
            market_snapshot=snapshot,
            market_release=InputRelease(release_id="market-only", manifest_sha256="a" * 64),
            economic_path=None,
            economic_identity=None,
            as_of=AS_OF,
            evaluation_time=evaluated_at,
            ttm_rules=DEFAULT_TTM_RULES,
            tickers=("1301",),
            financial_input=financial,
        ).materialize()
    runs, app, workspace = tmp_path / "runs", tmp_path / "app", tmp_path / "workspace"
    ModelGenerationStore(runs).publish(generated)
    draft = GeneratedResearchSelectionDraft(
        schema_version=2,
        request=ResearchRequest(
            text="財務を確認する", scope="specified_stocks", requested_tickers=("1301",)
        ),
        research_as_of=AS_OF,
        input_known_at=evaluated_at,
        generation_id=generated.generation_id,
        selection_summary="固定した財務と受領不足を確認する",
        targets=(selected_target("1301"),),
    )
    prepared = prepare_selected_workspace(
        draft=draft, db_path=app, workspace=workspace, runs_path=runs, now=evaluated_at
    )
    saved = _selection_from_cli(app, prepared.operation_id, capsys)
    original = yaml.safe_load((workspace / "domain-inputs.yaml").read_text())
    entry = original["analysis_source"]["inputs"]["financial"]
    assert entry["summary"] == expected_summary
    assert "descriptor" not in entry
    exact = _resolve(saved, entry["full_result_ref"])
    assert exact == financial.identity.descriptor
    assert hashlib.sha256(canonical_json(exact).encode()).hexdigest() == entry["sha256"]
    assert len(canonical_json(original["analysis_source"]).encode()) < 4000

    # A later source correction cannot change the earlier fixed membership or its
    # raw pack. No immutable copy of the complete daily head is published.
    owner.commit([_attempt("corrected", partial_day, [{**row, "Sales": 200}], at=evaluated_at)])
    assert owner.fixed().content_id != fixed.content_id
    assert model_input(store, fixed, as_of=AS_OF, evaluated_at=evaluated_at) == financial
    assert all("/heads/" not in key for key in store.values)
    runs.unlink()
    market.unlink()
    shutil.rmtree(native)
    shutil.rmtree(tmp_path / "mirror")
    (workspace / "domain-inputs.yaml").unlink()
    del store.values[HEAD_KEY]

    # The exact InputSnapshot exposed by the ordinary Operation CLI remains a
    # usable cold-rebuild input. Only its retained raw packs are needed here.
    saved_input = InputSnapshot.model_validate(saved["inputs"]["generation"]["inputs"]["financial"])
    reads_before = len(store.get_keys)
    cold = tmp_path / "cold"
    result = rebuild_saved_input(store, saved_input, start=START, end=AS_OF, output=cold)
    assert result == {"days": 4, "rows": 2, "unavailable": 1, "partial": 1, "incomplete": 2}
    assert HEAD_KEY not in store.get_keys[reads_before:]
    receipt_rows = pq.read_table(cold / "financial_receipt_status.parquet").to_pylist()
    assert {
        row["day"].isoformat(): {
            "status": row["projection_status"],
            "incomplete": row["incomplete_requests"],
        }
        for row in receipt_rows
    } == exact["receipt_status"]
    assert all(row["l1_coverage_status"] == "partial" for row in receipt_rows)
    assert [
        json.loads(row["document_payload"])["Sales"]
        for row in pq.read_table(cold / "financial_summaries.parquet").to_pylist()
    ] == [100, 100]
    assert json.loads((cold / "input.json").read_text())["saved_input"] == saved_input.model_dump(
        mode="json"
    )
    assert not (cold / "head.json").exists()

    def forbidden(*args, **kwargs):
        pytest.fail("saved Research must not reread source or generation caches")

    monkeypatch.setattr(FinancialL1, "fixed", forbidden)
    monkeypatch.setattr(store, "get_bytes", forbidden)
    monkeypatch.setattr(
        "baibai_engine.investment_judgment.workflow.generated_inputs.read_research_model_inputs",
        forbidden,
    )
    resumed = resume_selected_workspace(
        operation_id=prepared.operation_id, db_path=app, workspace=workspace, now=evaluated_at
    )
    assert resumed.selection_sha256 == prepared.selection_sha256
    rebound = _selection_from_cli(app, prepared.operation_id, capsys)
    assert rebound == saved
    assert _resolve(rebound, entry["full_result_ref"]) == exact
    assert yaml.safe_load((workspace / "domain-inputs.yaml").read_text()) == original
    assert not runs.exists()


def test_native_descriptor_and_generation_identity_cannot_be_substituted(tmp_path):
    from tests.helpers.retained_model_generation import seed_generation

    descriptor = {"summary": {"unavailable_days": 1}, "scopes": {"2026-11-01": {}}}
    identity = InputSnapshot(
        kind="native_l1",
        reference="financial",
        descriptor=descriptor,
        sha256=hashlib.sha256(canonical_json(descriptor).encode()).hexdigest(),
    )
    runs = tmp_path / "runs"
    generation = seed_generation(runs, now=AT, as_of=AS_OF).model_copy(
        update={"generation_id": "native-fixed", "inputs": {"financial": identity}}
    )
    ModelGenerationStore(runs).publish(generation)
    fixed = read_research_model_inputs(generation.generation_id, targets=("1234",), path=runs)
    forged = deepcopy(identity.model_dump(mode="json"))
    forged["descriptor"]["summary"]["unavailable_days"] = 0
    with pytest.raises(ValidationError, match="digest-matching fixed descriptor"):
        InputSnapshot.model_validate(forged)
    forged["sha256"] = hashlib.sha256(canonical_json(forged["descriptor"]).encode()).hexdigest()
    changed = InputSnapshot.model_validate(forged)
    with pytest.raises(GenerationConflictError, match="different content"):
        ModelGenerationStore(runs).publish(
            generation.model_copy(update={"inputs": {"financial": changed}})
        )
    with pytest.raises(ValueError, match="differs from its canonical generation"):
        verify_generated_inputs(
            fixed.model_copy(update={"inputs": {"financial": changed}}),
            targets=("1234",),
            runs_path=runs,
        )
