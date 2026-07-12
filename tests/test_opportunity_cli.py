"""Public CLI contract and golden-path tests for baibai-loop-opportunity.

These drive the CLI the way the runbook does — through scaffolds and the workspace,
never by copying a fixture packet as the operational input. Where a fully-ready
packet is needed (promotion), it is constructed with the public hashing API to
simulate the operator filling the draft from primary sources.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml

from baibai_loop.foundation.time import JST
from baibai_loop.foundation.yaml_io import safe_load
from baibai_loop.market.sqlite.schema import open_connection
from baibai_loop.thesis.close_source import (
    _EXPECTED_MARKET_SCHEMA_VERSION,
    resolve_previous_business_day_close,
)
from baibai_loop.thesis.decision_cli import main as decision_main
from baibai_loop.thesis.decision_packet import (
    DecisionPacketDocument,
    decision_packet_core_hash,
)
from baibai_loop.thesis.opportunity_cli import main as opportunity_main
from baibai_loop.validation.decision_packet import validate_decision_packet_file

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/decision-packet/2331-decision.yaml"
REVIEW_FIXTURE = ROOT / "tests/fixtures/decision-packet/2331-decision-review.yaml"
LEDGER_FIXTURE = ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"

FIXED_NOW = datetime(2026, 7, 12, 10, 0, tzinfo=JST)
TARGET_SESSION = "2026-07-13"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _seed_bars(
    sqlite_path: Path,
    rows: list[tuple[str, str, float | None, float | None]],
) -> None:
    """Insert (ticker, traded_at, close, adjustment_factor) rows into the store."""
    conn = open_connection(sqlite_path)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO jquants_daily_bars"
            "(ticker, traded_at, close, adjustment_factor) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _write_selection(path: Path, audit_pool: list[dict[str, object]]) -> None:
    payload = {
        "recommendations": [],
        "audit_pool": audit_pool,
        "selection": {
            "research_selection_target_max": 5,
            "research_selection_playbook_order": ["cashflow-yield-discount"],
        },
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _audit_row(ticker: str, rank: int = 1) -> dict[str, object]:
    return {
        "rank": rank,
        "ticker": ticker,
        "name": f"candidate {ticker}",
        "screening_playbook": "cashflow-yield-discount",
        "expected_return_pct": 9.5,
        "fair_value_anchor_yen": 1300,
        "market_price_yen": 1000,
        "liquidity_status": "pass",
        "durability_warnings": [],
        "event_warnings": [],
        "selection_reasons": ["cashflow-yield-discount"],
    }


def _prepared_workspace(tmp_path: Path, sqlite_path: Path, ticker: str = "2331") -> Path:
    selection = tmp_path / "selection.yaml"
    _write_selection(selection, [_audit_row(ticker)])
    workspace = tmp_path / "ws"
    assert (
        opportunity_main(
            [
                "prepare",
                "--asof",
                "2026-07-03",
                "--selection-output",
                str(selection),
                "--ledger",
                str(LEDGER_FIXTURE),
                "--workspace",
                str(workspace),
            ]
        )
        == 0
    )
    return workspace


def _ready_packet_and_review() -> tuple[dict[str, object], dict[str, object], str]:
    """Build an override-free ready packet + a matching bound review from the fixture.

    All permanent-loss axes are set to acceptable/verified so no human override is
    needed; the review's reviewed_packet_sha256 is bound to the packet core hash.
    """
    packet = safe_load(FIXTURE.read_text(encoding="utf-8"))
    for risk in packet["permanent_loss_risks"]:
        risk["assessment"] = "acceptable"
        risk["evidence_status"] = "verified"
    packet["judgment"]["permanent_loss_conclusion"] = "acceptable"
    packet.pop("human_evidence_override", None)
    review_filename = "2026-07-03-2331-decision-review.yaml"
    packet["independent_review_ref"] = review_filename

    document = DecisionPacketDocument.model_validate(packet)
    core_hash = decision_packet_core_hash(document)

    review = safe_load(REVIEW_FIXTURE.read_text(encoding="utf-8"))
    review["reviewed_packet_sha256"] = core_hash
    review["primary_source_check"] = "verified"
    return packet, review, review_filename


def _fill_ready_workspace(workspace: Path, ticker: str = "2331") -> str:
    """Simulate the operator filling a scaffolded draft with a ready packet+review."""
    selection_path = workspace / "selection.yaml"
    selection = safe_load(selection_path.read_text(encoding="utf-8"))
    selection["shortlist"] = [{"ticker": ticker, "reason": "primary-source research"}]
    selection_path.write_text(
        yaml.safe_dump(selection, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    comparison_path = workspace / "research-comparison.yaml"
    comparison = safe_load(comparison_path.read_text(encoding="utf-8"))
    comparison["selected_ticker"] = ticker
    comparison_path.write_text(
        yaml.safe_dump(comparison, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    packet, review, review_filename = _ready_packet_and_review()
    ticker_dir = workspace / ticker
    ticker_dir.mkdir(parents=True, exist_ok=True)
    (ticker_dir / "packet-draft.yaml").write_text(
        yaml.safe_dump(packet, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    (ticker_dir / "review-draft.yaml").write_text(
        yaml.safe_dump(review, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    checklist = {
        "checks": [
            {"check_id": check_id, "status": "complete", "source_ids": ["primary-results"]}
            for check_id in _checklist_ids()
        ]
    }
    (ticker_dir / "research-checklist.yaml").write_text(
        yaml.safe_dump(checklist, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return review_filename


def _checklist_ids() -> tuple[str, ...]:
    from baibai_loop.thesis.opportunity import CHECKLIST_IDS

    return CHECKLIST_IDS


def _run(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, object]]:
    code = opportunity_main(args, now=FIXED_NOW)
    out = capsys.readouterr().out
    payload = yaml.safe_load(out) if out.strip() else {}
    assert isinstance(payload, dict)
    return code, payload


# --------------------------------------------------------------------------- #
# prepare
# --------------------------------------------------------------------------- #


def test_close_source_expected_schema_version_tracks_market() -> None:
    # A market schema version bump changes SQLITE_SCHEMA_VERSION; this coupling
    # assertion turns that bump into a red CI check so the boundary-crossing schema
    # literals in close_source cannot drift silently.
    from baibai_loop.market.sqlite.schema import SQLITE_SCHEMA_VERSION

    assert _EXPECTED_MARKET_SCHEMA_VERSION == SQLITE_SCHEMA_VERSION


def test_close_source_degrades_on_schema_version_mismatch(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute(f"PRAGMA user_version = {_EXPECTED_MARKET_SCHEMA_VERSION + 999}")
        conn.commit()
    finally:
        conn.close()
    resolved = resolve_previous_business_day_close(
        sqlite_path=sqlite_path, ticker="2331", target_session=date(2026, 7, 13)
    )
    assert resolved is None


def test_prepare_annotates_held_reserved_without_excluding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    # 2331 is a holding in the representative ledger.
    workspace = _prepared_workspace(tmp_path, sqlite_path, ticker="2331")

    selection = safe_load((workspace / "selection.yaml").read_text(encoding="utf-8"))
    tickers = [row["ticker"] for row in selection["audit_pool"]]
    assert "2331" in tickers  # annotated, never excluded
    assert selection["audit_pool"][0]["portfolio_annotation"] in {
        "held",
        "reserved",
        "held_and_reserved",
    }


def test_prepare_empty_audit_pool_is_no_actionable_bargain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    selection = tmp_path / "selection.yaml"
    _write_selection(selection, [])
    code, payload = _run(
        [
            "prepare",
            "--asof",
            "2026-07-03",
            "--selection-output",
            str(selection),
            "--ledger",
            str(LEDGER_FIXTURE),
            "--workspace",
            str(tmp_path / "ws"),
        ],
        capsys,
    )
    assert code == 0
    assert payload["actionable"] is False
    assert payload["note"] == "no actionable bargain"


def test_status_allows_intentional_shortlist_edit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    selection_file = workspace / "selection.yaml"
    selection = safe_load(selection_file.read_text(encoding="utf-8"))
    selection["shortlist"] = [{"ticker": "2331", "reason": "primary-source research"}]
    selection_file.write_text(yaml.safe_dump(selection, sort_keys=False), encoding="utf-8")
    code = opportunity_main(["status", "--workspace", str(workspace)], now=FIXED_NOW)
    assert code == 0


def test_status_reports_external_input_hash_drift_as_exit_4(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    manifest = safe_load((workspace / "manifest.yaml").read_text(encoding="utf-8"))
    selection_file = Path(manifest["inputs"]["selection_output"]["path"])
    selection_file.write_text(
        selection_file.read_text(encoding="utf-8") + "\n# changed after prepare\n",
        encoding="utf-8",
    )
    code = opportunity_main(["status", "--workspace", str(workspace)], now=FIXED_NOW)
    assert code == 4


def test_status_rejects_shortlist_ticker_outside_audit_pool(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    selection_file = workspace / "selection.yaml"
    selection = safe_load(selection_file.read_text(encoding="utf-8"))
    selection["shortlist"] = [{"ticker": "9999", "reason": "not in audit pool"}]
    selection_file.write_text(yaml.safe_dump(selection, sort_keys=False), encoding="utf-8")
    code = opportunity_main(["status", "--workspace", str(workspace)], now=FIXED_NOW)
    assert code == 3


# --------------------------------------------------------------------------- #
# packet-scaffold
# --------------------------------------------------------------------------- #


def test_packet_scaffold_snapshots_raw_close(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(
        sqlite_path, [("2331", "2026-07-09", 990.0, 1.0), ("2331", "2026-07-10", 1005.0, 1.0)]
    )
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    code, payload = _run(
        [
            "packet-scaffold",
            "--workspace",
            str(workspace),
            "--ticker",
            "2331",
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
        ],
        capsys,
    )
    assert code == 0
    assert payload["close_yen"] == 1005.0
    assert payload["price_as_of"] == "2026-07-10"
    draft = safe_load((workspace / "2331" / "packet-draft.yaml").read_text(encoding="utf-8"))
    snapshot = draft["input_snapshot"]["price_snapshot"]
    assert snapshot["market_price_yen"] == 1005.0
    assert snapshot["price_basis"] == "raw_unadjusted_close"


def test_packet_scaffold_without_raw_close_exits_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    # Only an adjusted-only row (raw close NULL); the scaffold must not guess.
    _seed_bars(sqlite_path, [("2331", "2026-07-10", None, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    code = opportunity_main(
        [
            "packet-scaffold",
            "--workspace",
            str(workspace),
            "--ticker",
            "2331",
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
        ],
        now=FIXED_NOW,
    )
    assert code == 3


def test_packet_scaffold_defers_when_latest_bar_is_adjusted_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The latest business-day bar has a NULL raw close (adjusted-only); an older bar
    # has a raw close. D5 requires deferring on the missing latest close rather than
    # quietly using the stale older one.
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(
        sqlite_path,
        [("2331", "2026-07-09", 990.0, 1.0), ("2331", "2026-07-10", None, 1.0)],
    )
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    code = opportunity_main(
        [
            "packet-scaffold",
            "--workspace",
            str(workspace),
            "--ticker",
            "2331",
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
        ],
        now=FIXED_NOW,
    )
    assert code == 3


def test_packet_scaffold_blocks_checklist_on_corporate_action(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 0.5)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    code, payload = _run(
        [
            "packet-scaffold",
            "--workspace",
            str(workspace),
            "--ticker",
            "2331",
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
        ],
        capsys,
    )
    assert code == 0
    assert payload["corporate_action_unresolved"] is True
    checklist = safe_load(
        (workspace / "2331" / "research-checklist.yaml").read_text(encoding="utf-8")
    )
    corporate = next(
        item for item in checklist["checks"] if item["check_id"] == "source.corporate_action"
    )
    assert corporate["status"] == "blocked"


# --------------------------------------------------------------------------- #
# review-scaffold / promote
# --------------------------------------------------------------------------- #


def test_review_scaffold_goes_stale_when_packet_hash_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    _fill_ready_workspace(workspace)
    # Bind the review to the current packet hash.
    assert (
        opportunity_main(
            ["review-scaffold", "--workspace", str(workspace), "--ticker", "2331", "--force"],
            now=FIXED_NOW,
        )
        == 0
    )
    # Mutate the packet so the bound review is now stale.
    packet_path = workspace / "2331" / "packet-draft.yaml"
    packet = safe_load(packet_path.read_text(encoding="utf-8"))
    packet["judgment"]["strongest_countercase"] = "A different countercase after re-review."
    packet_path.write_text(yaml.safe_dump(packet, sort_keys=False), encoding="utf-8")
    # The scaffolded (null) review is not the ready fixture review, so refill the
    # ready review bound to the OLD hash to isolate the staleness gate.
    _, review, _ = _ready_packet_and_review()
    (workspace / "2331" / "review-draft.yaml").write_text(
        yaml.safe_dump(review, sort_keys=False), encoding="utf-8"
    )
    code = opportunity_main(
        [
            "promote",
            "--workspace",
            str(workspace),
            "--ticker",
            "2331",
            "--output-dir",
            str(tmp_path / "records/03-thesis/2026/07"),
        ],
        now=FIXED_NOW,
    )
    assert code == 3
    assert not (tmp_path / "records").exists() or not list(
        (tmp_path / "records").rglob("*-decision.yaml")
    )


def test_promote_refuses_when_checklist_pending(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    _fill_ready_workspace(workspace)
    # Reintroduce a pending check.
    checklist_path = workspace / "2331" / "research-checklist.yaml"
    checklist = safe_load(checklist_path.read_text(encoding="utf-8"))
    checklist["checks"][0]["status"] = "pending"
    checklist_path.write_text(yaml.safe_dump(checklist, sort_keys=False), encoding="utf-8")
    output_dir = tmp_path / "records/03-thesis/2026/07"
    code = opportunity_main(
        [
            "promote",
            "--workspace",
            str(workspace),
            "--ticker",
            "2331",
            "--output-dir",
            str(output_dir),
        ],
        now=FIXED_NOW,
    )
    assert code == 3
    assert not output_dir.exists() or not list(output_dir.glob("*.yaml"))


def test_promote_rejects_non_complete_checklist_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A hand-edited typo status ("complet") is not "complete" and must count as
    # unresolved so it cannot slip past the promotion gate.
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    _fill_ready_workspace(workspace)
    checklist_path = workspace / "2331" / "research-checklist.yaml"
    checklist = safe_load(checklist_path.read_text(encoding="utf-8"))
    checklist["checks"][0]["status"] = "complet"
    checklist_path.write_text(yaml.safe_dump(checklist, sort_keys=False), encoding="utf-8")
    output_dir = tmp_path / "records/03-thesis/2026/07"
    code = opportunity_main(
        [
            "promote",
            "--workspace",
            str(workspace),
            "--ticker",
            "2331",
            "--output-dir",
            str(output_dir),
        ],
        now=FIXED_NOW,
    )
    assert code == 3
    assert not output_dir.exists() or not list(output_dir.glob("*.yaml"))


def test_promote_ready_writes_two_validated_canonical_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    review_filename = _fill_ready_workspace(workspace)
    output_dir = tmp_path / "records/03-thesis/2026/07"

    code, _ = _run(
        [
            "promote",
            "--workspace",
            str(workspace),
            "--ticker",
            "2331",
            "--output-dir",
            str(output_dir),
        ],
        capsys,
    )
    assert code == 0
    packet_out = output_dir / "2026-07-03-2331-decision.yaml"
    review_out = output_dir / review_filename
    assert packet_out.exists()
    assert review_out.exists()

    findings = validate_decision_packet_file(packet_out)
    assert [finding.severity for finding in findings if finding.severity == "error"] == []
    assert decision_main([str(packet_out)]) == 0


def test_promote_never_overwrites_canonical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    _fill_ready_workspace(workspace)
    output_dir = tmp_path / "records/03-thesis/2026/07"
    args = [
        "promote",
        "--workspace",
        str(workspace),
        "--ticker",
        "2331",
        "--output-dir",
        str(output_dir),
    ]
    assert opportunity_main(args, now=FIXED_NOW) == 0
    # A second promotion of the same as-of never overwrites the canonical record.
    assert opportunity_main(args, now=FIXED_NOW) == 4


# --------------------------------------------------------------------------- #
# plan-limit
# --------------------------------------------------------------------------- #


def _promoted_packet(tmp_path: Path, sqlite_path: Path) -> Path:
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    _fill_ready_workspace(workspace)
    output_dir = tmp_path / "records/03-thesis/2026/07"
    assert (
        opportunity_main(
            [
                "promote",
                "--workspace",
                str(workspace),
                "--ticker",
                "2331",
                "--output-dir",
                str(output_dir),
            ],
            now=FIXED_NOW,
        )
        == 0
    )
    return output_dir / "2026-07-03-2331-decision.yaml"


def test_plan_limit_close_within_max_plans_limit_at_close(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    packet = _promoted_packet(tmp_path, sqlite_path)
    code, payload = _run(
        [
            "plan-limit",
            "--packet",
            str(packet),
            "--ledger",
            str(LEDGER_FIXTURE),
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
        ],
        capsys,
    )
    assert code == 0
    assert payload["status"] == "planned_limit"
    assert payload["limit_price_yen"] == payload["close_yen"] == 1000
    assert payload["price_basis"] == "raw_unadjusted_close"
    assert payload["expires_at"] == "2026-07-13T15:30:00+09:00"


def test_plan_limit_close_above_max_defers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 5000.0, 1.0)])
    packet = _promoted_packet(tmp_path, sqlite_path)
    code, payload = _run(
        [
            "plan-limit",
            "--packet",
            str(packet),
            "--ledger",
            str(LEDGER_FIXTURE),
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
        ],
        capsys,
    )
    assert code == 0  # defer is a normal judgment
    assert payload["status"] == "defer"
    assert payload["limit_price_yen"] is None
    assert payload["quantity"] == 0
    assert "close_above_max_acceptable_price" in payload["defer_reasons"]


def test_plan_limit_zero_close_defers_without_crashing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A corrupt zero close must defer (missing close), not raise DivisionByZero in
    # lot sizing and crash with a traceback.
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 0.0, 1.0)])
    packet = _promoted_packet(tmp_path, sqlite_path)
    code, payload = _run(
        [
            "plan-limit",
            "--packet",
            str(packet),
            "--ledger",
            str(LEDGER_FIXTURE),
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
        ],
        capsys,
    )
    assert code == 0
    assert payload["status"] == "defer"
    assert "latest_business_day_close_missing" in payload["defer_reasons"]


def test_plan_limit_corporate_action_defers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 0.5)])
    packet = _promoted_packet(tmp_path, sqlite_path)
    code, payload = _run(
        [
            "plan-limit",
            "--packet",
            str(packet),
            "--ledger",
            str(LEDGER_FIXTURE),
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
        ],
        capsys,
    )
    assert code == 0
    assert payload["status"] == "defer"
    assert "corporate_action_unresolved" in payload["defer_reasons"]


def test_plan_limit_single_lot_above_budget_max_still_proposes_with_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    packet = _promoted_packet(tmp_path, sqlite_path)
    # 1 lot = 1000 * 100 = 100,000 > budget_max 90,000.
    code, payload = _run(
        [
            "plan-limit",
            "--packet",
            str(packet),
            "--ledger",
            str(LEDGER_FIXTURE),
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
            "--budget-min-yen",
            "50000",
            "--budget-max-yen",
            "90000",
        ],
        capsys,
    )
    assert code == 0
    assert payload["status"] == "planned_limit"
    assert payload["quantity"] == 100  # a single board lot, never auto-rejected
    assert "budget_guide_exceeded" in payload["warnings"]


def test_plan_limit_quantity_never_overshoots_budget_max(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # lot_notional = 500.005 * 100 = 50,000.5; truncating to int (50,000) would let
    # 100,000 // 50,000 = 2 lots overshoot. The exact floor keeps it at one lot.
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 500.005, 1.0)])
    packet = _promoted_packet(tmp_path, sqlite_path)
    code, payload = _run(
        [
            "plan-limit",
            "--packet",
            str(packet),
            "--ledger",
            str(LEDGER_FIXTURE),
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
            "--budget-min-yen",
            "10000",
            "--budget-max-yen",
            "100000",
        ],
        capsys,
    )
    assert code == 0
    assert payload["status"] == "planned_limit"
    assert payload["quantity"] == 100
    assert payload["notional_yen"] <= 100000


def test_plan_limit_budget_and_warnings_do_not_change_limit_or_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    packet = _promoted_packet(tmp_path, sqlite_path)

    def run_with_budget(budget_max: str) -> dict[str, object]:
        code, payload = _run(
            [
                "plan-limit",
                "--packet",
                str(packet),
                "--ledger",
                str(LEDGER_FIXTURE),
                "--sqlite-path",
                str(sqlite_path),
                "--target-session",
                TARGET_SESSION,
                "--budget-max-yen",
                budget_max,
            ],
            capsys,
        )
        assert code == 0
        return payload

    small = run_with_budget("250000")
    large = run_with_budget("50000000")
    # Investment ranking / limit never move with budget; only quantity rounds.
    assert small["status"] == large["status"] == "planned_limit"
    assert small["limit_price_yen"] == large["limit_price_yen"] == 1000


def test_plan_limit_is_deterministic_across_clocks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    packet = _promoted_packet(tmp_path, sqlite_path)
    args = [
        "plan-limit",
        "--packet",
        str(packet),
        "--ledger",
        str(LEDGER_FIXTURE),
        "--sqlite-path",
        str(sqlite_path),
        "--target-session",
        TARGET_SESSION,
    ]
    capsys.readouterr()  # drain prepare/promote output from the buffer
    opportunity_main(args, now=datetime(2026, 7, 12, 10, 0, tzinfo=JST))
    first = yaml.safe_load(capsys.readouterr().out)
    opportunity_main(args, now=datetime(2026, 7, 12, 18, 30, tzinfo=JST))
    second = yaml.safe_load(capsys.readouterr().out)
    assert first == second
