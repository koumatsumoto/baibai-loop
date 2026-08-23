from __future__ import annotations

import ast
import re
import zipfile
from datetime import date
from io import BytesIO
from pathlib import Path

import pytest

from baibai_engine.screening.edinet_revision import (
    compute_extractor_revision,
    extraction_artifact_manifest,
)
from baibai_engine.screening.providers.edinet_csv import parse_csv_zip_metric_record
from baibai_engine.screening.schema import TTMQuality

_ENGINE_ROOT = Path(__file__).resolve().parents[2] / "engine/src/baibai_engine"

# The import closure of `screening.cli.edinet_extract`, which implements
# `extract-edinet-metrics`. Pinning it here is what makes a dependency appearing in or
# disappearing from the extraction path visible: the manifest decides when a reusable
# EDINET metric baseline is thrown away, so it must not drift silently in either
# direction. Every entry can change what an EDINET metric row says.
_EXPECTED_MANIFEST = (
    "market/jquants.py",
    "market/sqlite/convert.py",
    "market/ticker.py",
    "screening/cli/common.py",
    "screening/cli/edinet_extract.py",
    "screening/edinet_revision.py",
    "screening/edinet_store.py",
    "screening/metric_quality.py",
    "screening/providers/edinet.py",
    "screening/providers/edinet_csv.py",
    "screening/source_coverage.py",
    "screening/sqlite_cache/edinet.py",
    "screening/store_readiness.py",
)

# What the extraction reaches *outside* the tracked prefixes. Importing the entry also
# executes package `__init__` files that pull in far more than this, so "the extraction
# never imports it" is not the safety criterion — "the extraction never references a
# symbol from it" is. These are referenced and deliberately untracked:
#
# - `market.sqlite.schema` / `.migrations`: connections and schema version. A schema
#   change is already visible as a migration, not as a silently different row value.
# - `market.sqlite.coverage`: coverage bookkeeping the store write records. The baseline
#   read validates coverage with its own SQL (`edinet_store`), so a change here cannot
#   make a row's values wrong without also making the snapshot unreadable.
# - `market.sqlite`: the package facade, re-exports only.
# - `market.bars`: the daily-bar dataclasses `market.jquants` declares. No EDINET row
#   carries a bar.
#
# Pinning the set is what stops a value-affecting helper from being moved out of the
# manifest: the closure would then reach a fifth module and this test fails.
_UNTRACKED_REACHED_MODULES = frozenset(
    {
        "baibai_engine.market.bars",
        "baibai_engine.market.sqlite",
        "baibai_engine.market.sqlite.coverage",
        "baibai_engine.market.sqlite.migrations",
        "baibai_engine.market.sqlite.schema",
    }
)

# Screening modules whose symbols the extraction never references. They carry ranking,
# narrative, calibration, and the other providers' logic — all edited far more often than
# the extraction path itself, and none of it able to change a stored EDINET metric row.
# Several of them are still *imported* at runtime, because importing the entry executes
# `screening/cli/__init__.py`; what keeps them out of the manifest is that no symbol of
# theirs is named on the value path.
_UNREFERENCED_ARTIFACTS = (
    "screening/shortlist.py",
    "screening/shortlist_outcome.py",
    "screening/earnings_lag.py",
    "screening/cli/app.py",
    "screening/cli/cache.py",
    "screening/cli/providers.py",
    "screening/cli/query.py",
    "screening/cli/run.py",
    "screening/cli/prune.py",
    "screening/selection/payload.py",
    "screening/selection/candidate_diagnostics.py",
    "screening/calibration/evaluation.py",
    "screening/calibration/panel.py",
    # The candidate row model and the metrics computed from EDINET rows. Both read the
    # stored values; neither writes them.
    "screening/schema.py",
    "screening/metrics.py",
    "screening/margin_metrics.py",
    "screening/rule_config.py",
    # The other providers and their store slices.
    "screening/providers/jpx.py",
    "screening/providers/jquants.py",
    "screening/jpx_sources.py",
    "screening/master_snapshot.py",
    "screening/sqlite_cache/jpx.py",
    "screening/sqlite_cache/jquants.py",
    "screening/sqlite_reader.py",
    # Coverage verification, which reports what the store holds without producing rows.
    "screening/sqlite_coverage/core.py",
    "screening/sqlite_coverage/jpx.py",
    "screening/sqlite_coverage/jquants.py",
)


def _artifacts() -> dict[str, bytes]:
    return {path: f"source:{path}".encode() for path in extraction_artifact_manifest()}


def _edinet_csv_zip(rows: list[tuple[str, str, str]]) -> bytes:
    csv_text = "要素ID\tコンテキストID\t値\n" + "\n".join(
        f"{element}\t{context}\t{value}" for element, context, value in rows
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("XBRL_TO_CSV/sample.csv", csv_text.encode("utf-16"))
    return buffer.getvalue()


def test_manifest_matches_the_import_closure_of_the_extraction_entry() -> None:
    assert extraction_artifact_manifest() == _EXPECTED_MANIFEST
    assert re.fullmatch(r"[0-9a-f]{64}", compute_extractor_revision())


def test_manifest_excludes_screening_modules_the_extraction_never_references() -> None:
    manifest = set(extraction_artifact_manifest())
    for artifact in _UNREFERENCED_ARTIFACTS:
        assert (_ENGINE_ROOT / artifact).is_file(), f"{artifact} no longer exists"
        assert artifact not in manifest


def test_manifest_excludes_the_commands_that_share_the_cli_with_the_extraction() -> None:
    """The other cache commands cannot change what a stored EDINET metric row says.

    `bootstrap-cache`, `verify-cache-coverage` and `backfill-history` read J-Quants,
    JPX and coverage; the extraction reads EDINET. Sharing a module with them put all
    of that into the manifest, and three of six consecutive daily runs then paid a full
    re-download — twice for edits that touched no EDINET value at all. These are the
    modules those edits landed in.
    """
    manifest = set(extraction_artifact_manifest())
    for artifact in (
        "screening/cli/cache.py",
        "screening/cli/providers.py",
        "screening/sqlite_reader.py",
        "screening/providers/jpx.py",
        "screening/providers/jquants.py",
        "screening/sqlite_cache/jpx.py",
        "screening/sqlite_coverage/core.py",
        "screening/metrics.py",
        "screening/schema.py",
    ):
        assert (_ENGINE_ROOT / artifact).is_file(), f"{artifact} no longer exists"
        assert artifact not in manifest

    # The parsers that do decide a row's values stay in, so a real extraction change
    # still discards the baseline.
    for artifact in (
        "screening/providers/edinet.py",
        "screening/providers/edinet_csv.py",
        "screening/sqlite_cache/edinet.py",
        "screening/edinet_store.py",
        "screening/metric_quality.py",
    ):
        assert artifact in manifest


def test_manifest_modules_reach_dependencies_only_through_static_imports() -> None:
    """A derived manifest stays sound only while every dependency is a literal import.

    `importlib.import_module` / `__import__` would let the extraction path execute a
    module the parsed import graph never sees, leaving a stale metric row reusable
    after that module changed.
    """
    dynamic: list[str] = []
    for artifact in extraction_artifact_manifest():
        for node in ast.walk(ast.parse((_ENGINE_ROOT / artifact).read_bytes())):
            if not isinstance(node, ast.Call):
                continue
            called = node.func
            name = (
                called.attr
                if isinstance(called, ast.Attribute)
                else called.id
                if isinstance(called, ast.Name)
                else ""
            )
            if name in {"import_module", "__import__"}:
                dynamic.append(f"{artifact}:{node.lineno}")
    assert dynamic == []


def test_extractor_revision_changes_when_any_manifest_artifact_changes() -> None:
    original = _artifacts()
    original_revision = compute_extractor_revision(original)

    for path in extraction_artifact_manifest():
        changed = {**original, path: original[path] + b"\nchange"}
        assert compute_extractor_revision(changed) != original_revision


def test_extractor_revision_rejects_a_source_outside_the_manifest() -> None:
    artifacts = _artifacts()
    artifacts["screening/shortlist.py"] = b"source:screening/shortlist.py"
    with pytest.raises(ValueError, match="manifest mismatch"):
        compute_extractor_revision(artifacts)


def test_extractor_revision_rejects_manifest_drift() -> None:
    artifacts = _artifacts()
    artifacts.pop(extraction_artifact_manifest()[0])
    with pytest.raises(ValueError, match="manifest mismatch"):
        compute_extractor_revision(artifacts)


def test_metric_values_stay_pinned_to_the_narrowed_manifest() -> None:
    """The values the narrowed manifest answers for, fixed against an offline fixture.

    Together with the exclusion test this keeps the narrowing honest: every row below is
    produced by manifest sources alone, so a change in an excluded module cannot move
    them and reusing the baseline across such a change is safe.
    """
    record = parse_csv_zip_metric_record(
        ticker="7203",
        doc_id="S100REVISION",
        doc_type_code="120",
        content=_edinet_csv_zip(
            [
                ("jpcrp_cor:NetSales", "CurrentYearConsolidatedDuration", "1000"),
                (
                    "jpcrp_cor:CashFlowsFromOperatingActivities",
                    "CurrentYearConsolidatedDuration",
                    "150",
                ),
                ("jpcrp_cor:OperatingProfit", "CurrentYearConsolidatedDuration", "90"),
                (
                    "jpcrp_cor:DepreciationAndAmortization",
                    "CurrentYearConsolidatedDuration",
                    "30",
                ),
                (
                    "jpcrp_cor:PurchaseOfPropertyPlantAndEquipment",
                    "CurrentYearConsolidatedDuration",
                    "-40",
                ),
                ("jpcrp_cor:CashAndDeposits", "CurrentYearConsolidatedInstant", "300"),
                ("jppfs_cor:InvestmentSecurities", "CurrentYearConsolidatedInstant", "250"),
                ("jpcrp_cor:ShortTermBorrowings", "CurrentYearConsolidatedInstant", "20"),
                ("jpcrp_cor:LongTermBorrowings", "CurrentYearConsolidatedInstant", "50"),
                ("jpcrp_cor:Equity", "CurrentYearConsolidatedInstant", "800"),
                ("jpcrp_cor:TotalAssets", "CurrentYearConsolidatedInstant", "1400"),
            ]
        ),
        submit_datetime="2026-04-01 12:00",
        period_start=date(2025, 4, 1),
        period_end=date(2026, 3, 31),
    )

    assert record.ticker == "7203"
    assert record.source_doc_id == "S100REVISION"
    assert record.sales_ttm == 1000.0
    assert record.ocf_ttm == 150.0
    assert record.capex_ttm == 40.0
    assert record.fcf_ttm == 110.0
    assert record.ebitda_ttm == 120.0
    assert record.cash == 300.0
    assert record.debt == 70.0
    assert record.net_cash == 230.0
    assert record.investment_securities == 250.0
    assert record.equity == 800.0
    assert record.total_assets == 1400.0
    assert record.ttm_quality_fcf is TTMQuality.EXACT
    assert record.failure_reasons == ()


def test_the_untracked_part_of_the_closure_is_exactly_the_reviewed_set() -> None:
    """A value-affecting helper must not leave the manifest by changing package.

    The manifest only hashes `_TRACKED_PREFIXES`, so moving a predicate or a coercion
    into an untracked package removes it from the revision without changing the
    manifest, the revision, or any other test. Pinning what the extraction reaches
    outside those prefixes is what makes such a move fail.
    """
    from baibai_engine.screening import edinet_revision as revision

    visited: set[str] = set()
    untracked: set[str] = set()
    pending = list(revision._ENTRY_MODULES)
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        resolved = revision._resolve_module(module)
        if resolved is None:
            continue
        if not revision._is_tracked(module):
            untracked.add(module)
            continue
        visited.add(module)
        path, is_package = resolved
        source = revision._artifact(path).read_bytes()
        pending.extend(revision._imported_modules(source, module=module, is_package=is_package))

    assert untracked == set(_UNTRACKED_REACHED_MODULES)
