"""Public CLI contract and golden-path tests for baibai-engine research.

These drive the CLI the way the runbook does — through scaffolds and the workspace,
never by copying a fixture packet as the operational input. Where a fully-ready
packet is needed (promotion), it is constructed with the public hashing API to
simulate the operator filling the draft from primary sources.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml

from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.market.sqlite.schema import open_connection
from baibai_engine.research.close_source import (
    _EXPECTED_MARKET_SCHEMA_VERSION,
    resolve_holding_close_on_basis,
    resolve_previous_business_day_close,
)
from baibai_engine.research.decision_packet import (
    DecisionPacketDocument,
    IndependentReview,
    ScreeningEstimate,
    decision_packet_core_hash,
    evaluate_decision_packet,
)
from baibai_engine.research.opportunity_cli import main as opportunity_main
from baibai_engine.validation.decision_packet import validate_decision_packet_file

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


def _ledger_with_observed_at(
    tmp_path: Path,
    observed_at: str,
    *,
    active_candidate_reservation: bool = False,
    active_same_scope_reservation: bool = False,
    empty_other_reservation_factor: bool = False,
) -> Path:
    payload = safe_load(LEDGER_FIXTURE.read_text(encoding="utf-8"))
    payload["market_prices"][0]["observed_at"] = observed_at
    payload["market_prices"][0]["source_kind"] = "licensed_dataset"
    payload["market_prices"][0]["price_basis"] = "unadjusted_close"
    payload["market_prices"][0]["source_ref"] = "offline-test:raw-close:2331"
    if empty_other_reservation_factor:
        for event in payload["events"]:
            if event.get("ticker") == "8929" and "common_factors" in event:
                event["common_factors"] = []
    if active_candidate_reservation:
        for event in payload["events"]:
            if event.get("ticker") == "2331" and "common_factors" in event:
                event["common_factors"] = []
        payload["events"].append(
            {
                "event_id": "reserve-2331-proposal-test",
                "type": "reservation",
                "occurred_at": "2026-07-09T09:00:00+09:00",
                "reservation_id": "reservation-2331-proposal-test",
                "order_id": "order-2331-proposal-test",
                "ticker": "2331",
                "sector": "サービス業",
                "common_factors": [],
                "quantity": 100,
                "price_guard_yen": 1000,
                "expires_at": "2026-07-31T15:30:00+09:00",
            }
        )
    if active_same_scope_reservation:
        payload["events"].append(
            {
                "event_id": "reserve-9999-same-scope-test",
                "type": "reservation",
                "occurred_at": "2026-07-09T09:00:00+09:00",
                "reservation_id": "reservation-9999-same-scope-test",
                "order_id": "order-9999-same-scope-test",
                "ticker": "9999",
                "sector": "サービス業",
                "common_factors": ["labor-automation"],
                "quantity": 100,
                "price_guard_yen": 1000,
                "expires_at": "2026-07-31T15:30:00+09:00",
            }
        )
    path = tmp_path / "test-ledger.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def _write_selection(
    path: Path,
    audit_pool: list[dict[str, object]],
    *,
    research_selection_target_max: object = 5,
    selection_asof: str | None = "2026-07-03",
) -> None:
    selection_metadata: dict[str, object] = {
        "research_selection_target_max": research_selection_target_max,
        "research_selection_playbook_order": ["cashflow-yield-discount"],
    }
    if selection_asof is not None:
        selection_metadata["asof"] = selection_asof
    payload = {
        "recommendations": [],
        "audit_pool": audit_pool,
        "selection": selection_metadata,
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


def _audit_row_with_estimate(
    ticker: str,
    rank: int = 1,
    *,
    annual: object = 0.095,
    expected_return_unit: object = "annual_ratio",
    sector_anchor: object = 1350.0,
    self_anchor: object = 1300.0,
) -> dict[str, object]:
    row = _audit_row(ticker, rank)
    row["estimate_snapshot"] = {
        "as_of": "2026-07-03",
        "expected_return": {
            "annual": annual,
            "origin": "estimate",
            "model_version": "expected-return-v1",
            "unit": expected_return_unit,
            "assumptions": "fixed screening estimate assumptions",
        },
        "fair_value": {
            "anchors": {
                "fv_sector_median_yen": sector_anchor,
                "fv_self_range_yen": self_anchor,
            },
            "origin": "estimate",
            "model_version": "expected-return-v1",
            "unit": "JPY_per_share",
            "assumptions": "fixed screening estimate assumptions",
        },
    }
    row["expected_return_pct"] = (
        round(float(annual) * 100, 4) if isinstance(annual, int | float) else 9.5
    )
    anchors = [
        float(value)
        for value in (sector_anchor, self_anchor)
        if isinstance(value, int | float) and not isinstance(value, bool)
    ]
    row["fair_value_anchor_yen"] = round(min(anchors), 4) if anchors else None
    return row


def _prepared_workspace(
    tmp_path: Path,
    sqlite_path: Path,
    ticker: str = "2331",
    *,
    audit_pool: list[dict[str, object]] | None = None,
) -> Path:
    selection = tmp_path / "selection.yaml"
    _write_selection(selection, audit_pool or [_audit_row(ticker)])
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
    workspace_selection = workspace / "selection.yaml"
    selection_doc = safe_load(workspace_selection.read_text(encoding="utf-8"))
    selection_doc["shortlist"] = [{"ticker": ticker, "reason": "primary-source research"}]
    workspace_selection.write_text(
        yaml.safe_dump(selection_doc, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return workspace


def _ledger_with_market_price_date(path: Path, observed_on: date) -> None:
    payload = safe_load(LEDGER_FIXTURE.read_text(encoding="utf-8"))
    payload["market_prices"][0]["observed_at"] = f"{observed_on.isoformat()}T15:30:00+09:00"
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


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


def _fill_ready_workspace(
    workspace: Path,
    ticker: str = "2331",
    *,
    preserve_screening_estimate: bool = False,
) -> str:
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
    if preserve_screening_estimate:
        scaffold = safe_load((ticker_dir / "packet-draft.yaml").read_text(encoding="utf-8"))
        scaffold_snapshot = scaffold["input_snapshot"]
        screening_estimate = scaffold_snapshot["screening_estimate"]
        screening_source = next(
            source
            for source in scaffold_snapshot["sources"]
            if source["source_id"] == "screening_selection"
        )
        packet["input_snapshot"]["sources"].append(screening_source)
        packet["input_snapshot"]["screening_estimate"] = screening_estimate
        packet["estimates"]["current_fair_value_yen"] = 1481.9088
        packet["estimates"]["screening_fv_bridge"] = {
            "primary_driver": "other",
            "note": "Research kept the screening FV anchor with no material revision.",
        }
        packet["judgment"]["proposed_at"] = FIXED_NOW.isoformat()
        review["reviewed_at"] = FIXED_NOW.isoformat()
        review["reviewed_packet_sha256"] = decision_packet_core_hash(
            DecisionPacketDocument.model_validate(packet)
        )
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
    from baibai_engine.research.opportunity import CHECKLIST_IDS

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
    from baibai_engine.market.sqlite.schema import SQLITE_SCHEMA_VERSION

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


def test_close_source_never_uses_older_ticker_bar_when_market_wide_date_is_missing(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(
        sqlite_path,
        [
            ("2331", "2026-07-09", 990.0, 1.0),
            ("9999", "2026-07-10", 500.0, 1.0),
        ],
    )

    resolved = resolve_previous_business_day_close(
        sqlite_path=sqlite_path, ticker="2331", target_session=date(2026, 7, 13)
    )

    assert resolved is None


def test_close_source_treats_missing_adjustment_factor_as_unresolved(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, None)])

    resolved = resolve_previous_business_day_close(
        sqlite_path=sqlite_path, ticker="2331", target_session=date(2026, 7, 13)
    )

    assert resolved is not None
    assert resolved.corporate_action_unresolved is True


@pytest.mark.parametrize(
    ("intermediate_close", "intermediate_factor"),
    [(None, 1.0), (995.0, None), (995.0, 0.5)],
    ids=("missing-bar", "missing-factor", "non-unit-factor"),
)
def test_holding_close_falls_back_when_revaluation_chain_is_incomplete(
    tmp_path: Path,
    intermediate_close: float | None,
    intermediate_factor: float | None,
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    rows: list[tuple[str, str, float | None, float | None]] = [
        ("2331", "2026-07-08", 990.0, 1.0),
        ("2331", "2026-07-10", 1000.0, 1.0),
    ]
    if intermediate_close is None:
        rows.append(("9999", "2026-07-09", 500.0, 1.0))
    else:
        rows.append(("2331", "2026-07-09", intermediate_close, intermediate_factor))
    _seed_bars(sqlite_path, rows)

    resolved = resolve_holding_close_on_basis(
        sqlite_path=sqlite_path,
        ticker="2331",
        ledger_price_observed_on=date(2026, 7, 8),
        basis_as_of=date(2026, 7, 10),
    )

    assert resolved is None


def test_holding_close_uses_exact_basis_after_complete_raw_chain(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(
        sqlite_path,
        [
            ("2331", "2026-07-09", 990.0, 1.0),
            ("2331", "2026-07-10", 1000.0, 1.0),
        ],
    )

    resolved = resolve_holding_close_on_basis(
        sqlite_path=sqlite_path,
        ticker="2331",
        ledger_price_observed_on=date(2026, 7, 9),
        basis_as_of=date(2026, 7, 10),
    )

    assert resolved is not None
    assert resolved.price_as_of == date(2026, 7, 10)
    assert resolved.close_yen == 1000.0


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


def test_holding_prepare_builds_fixed_one_ticker_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    ledger = tmp_path / "ledger.yaml"
    _ledger_with_market_price_date(ledger, date(2026, 7, 10))
    workspace = tmp_path / "holding-ws"
    code, payload = _run(
        [
            "holding-prepare",
            "--asof",
            "2026-07-10",
            "--ledger",
            str(ledger),
            "--ticker",
            "2331",
            "--workspace",
            str(workspace),
        ],
        capsys,
    )

    assert code == 0
    assert payload["audit_pool_size"] == 1
    manifest = safe_load((workspace / "manifest.yaml").read_text(encoding="utf-8"))
    selection = safe_load((workspace / "selection.yaml").read_text(encoding="utf-8"))
    comparison = safe_load((workspace / "research-comparison.yaml").read_text(encoding="utf-8"))
    assert manifest["purpose"] == "holding_review"
    assert manifest["holding_ticker"] == "2331"
    assert set(manifest["inputs"]) == {"ledger"}
    assert [row["ticker"] for row in selection["audit_pool"]] == ["2331"]
    assert [row["ticker"] for row in selection["shortlist"]] == ["2331"]
    assert comparison["selected_ticker"] == "2331"
    assert [row["ticker"] for row in comparison["candidates"]] == ["2331"]
    assert (
        opportunity_main(
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
            ]
        )
        == 0
    )
    packet = safe_load((workspace / "2331/packet-draft.yaml").read_text(encoding="utf-8"))
    assert packet["input_snapshot"]["as_of"] == "2026-07-10"
    assert packet["input_snapshot"]["sources"][0]["as_of"] == "2026-07-10"
    assert packet["input_snapshot"]["facts"][0]["as_of"] == "2026-07-10"


@pytest.mark.parametrize("ticker", ["8929", "9999"])
def test_holding_prepare_rejects_ticker_without_open_holding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], ticker: str
) -> None:
    code = opportunity_main(
        [
            "holding-prepare",
            "--asof",
            "2026-07-11",
            "--ledger",
            str(LEDGER_FIXTURE),
            "--ticker",
            ticker,
            "--workspace",
            str(tmp_path / "holding-ws"),
        ]
    )

    assert code == 3
    assert "not an open holding" in capsys.readouterr().err
    assert not (tmp_path / "holding-ws").exists()


def test_holding_workspace_detects_ledger_hash_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ledger.yaml"
    ledger.write_bytes(LEDGER_FIXTURE.read_bytes())
    workspace = tmp_path / "holding-ws"
    assert (
        opportunity_main(
            [
                "holding-prepare",
                "--asof",
                "2026-07-11",
                "--ledger",
                str(ledger),
                "--ticker",
                "2331",
                "--workspace",
                str(workspace),
            ]
        )
        == 0
    )
    capsys.readouterr()
    ledger.write_text(ledger.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    assert opportunity_main(["status", "--workspace", str(workspace)]) == 4
    assert "input hash drift" in capsys.readouterr().err


def test_holding_workspace_uses_exact_byte_hash_for_crlf_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ledger-crlf.yaml"
    ledger.write_bytes(LEDGER_FIXTURE.read_bytes().replace(b"\n", b"\r\n"))
    workspace = tmp_path / "holding-ws"

    assert (
        opportunity_main(
            [
                "holding-prepare",
                "--asof",
                "2026-07-11",
                "--ledger",
                str(ledger),
                "--ticker",
                "2331",
                "--workspace",
                str(workspace),
            ]
        )
        == 0
    )
    capsys.readouterr()
    manifest = safe_load((workspace / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["inputs"]["ledger"]["sha256"] == hashlib.sha256(ledger.read_bytes()).hexdigest()
    assert opportunity_main(["status", "--workspace", str(workspace)]) == 0


def test_holding_prepare_requires_same_day_market_price(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = opportunity_main(
        [
            "holding-prepare",
            "--asof",
            "2026-07-10",
            "--ledger",
            str(LEDGER_FIXTURE),
            "--ticker",
            "2331",
            "--workspace",
            str(tmp_path / "holding-ws"),
        ]
    )

    assert code == 3
    assert "market price date" in capsys.readouterr().err
    assert not (tmp_path / "holding-ws").exists()


@pytest.mark.parametrize(
    ("configured_max", "expected_slots"),
    [(3, 3), (0, 4)],
)
def test_prepare_derives_shortlist_slots_from_selection_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    configured_max: int,
    expected_slots: int,
) -> None:
    selection = tmp_path / "selection.yaml"
    _write_selection(
        selection,
        [_audit_row(str(1000 + index), rank=index + 1) for index in range(4)],
        research_selection_target_max=configured_max,
    )
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
    assert payload["shortlist_slots"] == expected_slots

    workspace_selection = safe_load(
        (tmp_path / "ws" / "selection.yaml").read_text(encoding="utf-8")
    )
    assert workspace_selection["shortlist_slots"] == expected_slots


@pytest.mark.parametrize("invalid_max", [None, -1, True, "5"])
def test_prepare_rejects_invalid_research_selection_target_max(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    invalid_max: object,
) -> None:
    selection = tmp_path / "selection.yaml"
    _write_selection(
        selection,
        [_audit_row("2331")],
        research_selection_target_max=invalid_max,
    )
    code = opportunity_main(
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
        ]
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "research_selection_target_max" in captured.err
    assert not (tmp_path / "ws").exists()


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


def test_status_waits_for_human_shortlist_before_packet_scaffold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    selection_path = workspace / "selection.yaml"
    selection = safe_load(selection_path.read_text(encoding="utf-8"))
    selection["shortlist"] = []
    selection_path.write_text(
        yaml.safe_dump(selection, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    code, payload = _run(["status", "--workspace", str(workspace)], capsys)

    assert code == 0
    assert payload["workspace_status"] == "awaiting_primary_research_selection"
    assert "candidate-report" in str(payload["next_command"])


def test_status_points_to_first_missing_shortlist_lane(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    selection_path = workspace / "selection.yaml"
    selection = safe_load(selection_path.read_text(encoding="utf-8"))
    selection["shortlist"] = [{"ticker": "2331", "reason": "research"}]
    selection_path.write_text(
        yaml.safe_dump(selection, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    code, payload = _run(["status", "--workspace", str(workspace)], capsys)

    assert code == 0
    assert payload["workspace_status"] == "incomplete"
    assert payload["next_command"] == "baibai-engine research packet-scaffold --ticker 2331"


def test_status_waits_for_all_lane_checks_before_comparison(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    assert (
        opportunity_main(
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
        == 0
    )
    capsys.readouterr()

    code, payload = _run(["status", "--workspace", str(workspace)], capsys)

    assert code == 0
    assert payload["workspace_status"] == "incomplete"
    assert payload["pending_checks"]
    assert payload["next_command"] == "complete primary research lane for 2331"

    checklist_path = workspace / "2331" / "research-checklist.yaml"
    checklist = safe_load(checklist_path.read_text(encoding="utf-8"))
    for check in checklist["checks"]:
        check["status"] = "complete"
    checklist_path.write_text(
        yaml.safe_dump(checklist, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    packet, _review, _review_filename = _ready_packet_and_review()
    (workspace / "2331" / "packet-draft.yaml").write_text(
        yaml.safe_dump(packet, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    code, payload = _run(["status", "--workspace", str(workspace)], capsys)

    assert code == 0
    assert payload["workspace_status"] == "ready_for_comparison"


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


def test_packet_scaffold_requires_primary_research_set_membership(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    selection_path = workspace / "selection.yaml"
    selection = safe_load(selection_path.read_text(encoding="utf-8"))
    selection["shortlist"] = []
    selection_path.write_text(
        yaml.safe_dump(selection, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

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
        ]
    )

    assert code == 3
    assert "primary-research set" in capsys.readouterr().err
    assert not (workspace / "2331").exists()


def test_packet_scaffold_confines_research_lane_to_direct_ticker_child(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    selection_output = tmp_path / "selection.yaml"
    _write_selection(selection_output, [_audit_row("../outside")])
    workspace = tmp_path / "ws"
    assert (
        opportunity_main(
            [
                "prepare",
                "--asof",
                "2026-07-03",
                "--selection-output",
                str(selection_output),
                "--ledger",
                str(LEDGER_FIXTURE),
                "--workspace",
                str(workspace),
            ]
        )
        == 0
    )
    capsys.readouterr()
    selection_path = workspace / "selection.yaml"
    selection = safe_load(selection_path.read_text(encoding="utf-8"))
    selection["shortlist"] = [{"ticker": "../outside", "reason": "invalid path"}]
    selection_path.write_text(
        yaml.safe_dump(selection, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    code = opportunity_main(
        [
            "packet-scaffold",
            "--workspace",
            str(workspace),
            "--ticker",
            "../outside",
            "--sqlite-path",
            str(tmp_path / "market.sqlite"),
            "--target-session",
            TARGET_SESSION,
        ]
    )

    assert code == 3
    assert "direct child" in capsys.readouterr().err
    assert not (tmp_path / "outside").exists()


def test_primary_research_lanes_share_lineage_and_remain_isolated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(
        sqlite_path,
        [("2331", "2026-07-10", 1000.0, 1.0), ("8929", "2026-07-10", 750.0, 1.0)],
    )
    selection_output = tmp_path / "selection.yaml"
    _write_selection(selection_output, [_audit_row("2331"), _audit_row("8929", rank=2)])
    workspace = tmp_path / "ws"
    assert (
        opportunity_main(
            [
                "prepare",
                "--asof",
                "2026-07-03",
                "--selection-output",
                str(selection_output),
                "--ledger",
                str(LEDGER_FIXTURE),
                "--workspace",
                str(workspace),
            ]
        )
        == 0
    )
    selection_path = workspace / "selection.yaml"
    selection = safe_load(selection_path.read_text(encoding="utf-8"))
    selection["shortlist"] = [
        {"ticker": "2331", "reason": "primary-source research"},
        {"ticker": "8929", "reason": "primary-source research"},
    ]
    selection_path.write_text(
        yaml.safe_dump(selection, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    manifest_before = (workspace / "manifest.yaml").read_bytes()

    for ticker in ("2331", "8929"):
        assert (
            opportunity_main(
                [
                    "packet-scaffold",
                    "--workspace",
                    str(workspace),
                    "--ticker",
                    ticker,
                    "--sqlite-path",
                    str(sqlite_path),
                    "--target-session",
                    TARGET_SESSION,
                ]
            )
            == 0
        )
        capsys.readouterr()

    manifest = safe_load(manifest_before.decode("utf-8"))
    assert (
        manifest["inputs"]["selection_output"]["sha256"]
        == hashlib.sha256(selection_output.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    )
    assert (
        manifest["inputs"]["ledger"]["sha256"]
        == hashlib.sha256(LEDGER_FIXTURE.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    )
    assert (workspace / "manifest.yaml").read_bytes() == manifest_before
    first_packet = safe_load((workspace / "2331" / "packet-draft.yaml").read_text("utf-8"))
    second_packet_path = workspace / "8929" / "packet-draft.yaml"
    second_packet_before = second_packet_path.read_bytes()
    assert first_packet["input_snapshot"]["ticker"] == "2331"
    assert safe_load(second_packet_before.decode("utf-8"))["input_snapshot"]["ticker"] == "8929"

    assert (
        opportunity_main(
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
                "--force",
            ]
        )
        == 0
    )
    assert second_packet_path.read_bytes() == second_packet_before


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
    assert payload["screening_estimate_transferred"] is False
    assert payload["screening_estimate_transfer_reason"] == "estimate_snapshot_missing"
    draft = safe_load((workspace / "2331" / "packet-draft.yaml").read_text(encoding="utf-8"))
    snapshot = draft["input_snapshot"]
    # The raw close is emitted as the single schema-valid market_price fact, not a
    # bespoke price_snapshot block. price_basis uses the canonical schema enum.
    assert "price_snapshot" not in snapshot
    facts = snapshot["facts"]
    assert len(facts) == 1
    fact = facts[0]
    assert fact["fact_kind"] == "market_price"
    assert fact["value"] == 1005.0
    assert fact["unit"] == "JPY"
    assert fact["as_of"] == "2026-07-10"
    assert fact["observed_at"] == "2026-07-10T15:30:00+09:00"
    assert fact["price_basis"] == "last_close_unadjusted"
    # The fact references a declared local_data source.
    assert fact["source_ids"] == [snapshot["sources"][0]["source_id"]]
    assert "screening_estimate" not in snapshot


def test_packet_scaffold_transfers_raw_screening_estimate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    workspace = _prepared_workspace(
        tmp_path,
        sqlite_path,
        audit_pool=[_audit_row_with_estimate("2331")],
    )
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
    assert payload["screening_estimate_transferred"] is True
    assert payload["screening_estimate_transfer_reason"] is None
    draft = safe_load((workspace / "2331" / "packet-draft.yaml").read_text(encoding="utf-8"))
    snapshot = draft["input_snapshot"]
    assert snapshot["screening_estimate"] == {
        "origin": "estimate",
        "model_version": "expected-return-v1",
        "as_of": "2026-07-03",
        "expected_return_annual_ratio": 0.095,
        "expected_return_unit": "annual_ratio",
        "fair_value_anchor_yen": 1300.0,
        "fair_value_unit": "JPY_per_share",
        "assumptions": "fixed screening estimate assumptions",
        "source_ids": ["screening_selection"],
    }
    source = next(
        item for item in snapshot["sources"] if item["source_id"] == "screening_selection"
    )
    assert source == {
        "source_id": "screening_selection",
        "ticker": "2331",
        "source_tier": "local_data",
        "provider": "baibai-loop",
        "dataset": "screening-selection",
        "retrieved_at": FIXED_NOW.isoformat(),
        "as_of": "2026-07-03",
        "used_for": "screening expected return and fair value anchor",
    }

    force_code, force_payload = _run(
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
            "--force",
        ],
        capsys,
    )
    assert force_code == 0
    assert force_payload["screening_estimate_transferred"] is True
    regenerated = safe_load((workspace / "2331" / "packet-draft.yaml").read_text(encoding="utf-8"))
    assert regenerated["input_snapshot"]["screening_estimate"] == snapshot["screening_estimate"]


def test_packet_scaffold_reads_hash_bound_selection_not_editable_audit_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    workspace = _prepared_workspace(
        tmp_path,
        sqlite_path,
        audit_pool=[_audit_row_with_estimate("2331")],
    )
    workspace_selection_path = workspace / "selection.yaml"
    editable = safe_load(workspace_selection_path.read_text(encoding="utf-8"))
    editable_row = editable["audit_pool"][0]
    editable_row["expected_return_pct"] = 50.0
    editable_row["fair_value_anchor_yen"] = 9999.0
    editable_row["estimate_snapshot"]["expected_return"]["annual"] = 0.5
    editable_row["estimate_snapshot"]["fair_value"]["anchors"] = {
        "fv_sector_median_yen": 9999.0,
        "fv_self_range_yen": 10000.0,
    }
    workspace_selection_path.write_text(
        yaml.safe_dump(editable, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

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
    assert payload["screening_estimate_transferred"] is True
    draft = safe_load((workspace / "2331" / "packet-draft.yaml").read_text(encoding="utf-8"))
    estimate = draft["input_snapshot"]["screening_estimate"]
    assert estimate["expected_return_annual_ratio"] == 0.095
    assert estimate["fair_value_anchor_yen"] == 1300.0


def test_packet_scaffold_keeps_null_fair_value_without_inventing_anchor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    workspace = _prepared_workspace(
        tmp_path,
        sqlite_path,
        audit_pool=[_audit_row_with_estimate("2331", sector_anchor=None, self_anchor=None)],
    )
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
    assert payload["screening_estimate_transferred"] is True
    draft = safe_load((workspace / "2331" / "packet-draft.yaml").read_text(encoding="utf-8"))
    assert draft["input_snapshot"]["screening_estimate"]["fair_value_anchor_yen"] is None


def test_packet_scaffold_quantizes_screening_anchor_to_packet_precision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    workspace = _prepared_workspace(
        tmp_path,
        sqlite_path,
        audit_pool=[
            _audit_row_with_estimate("2331", sector_anchor=1350.0, self_anchor=1300.123456)
        ],
    )

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
    assert payload["screening_estimate_transferred"] is True
    draft = safe_load((workspace / "2331" / "packet-draft.yaml").read_text(encoding="utf-8"))
    estimate = draft["input_snapshot"]["screening_estimate"]
    assert estimate["fair_value_anchor_yen"] == 1300.1235
    ScreeningEstimate.model_validate(estimate)


@pytest.mark.parametrize(
    "row",
    [
        _audit_row_with_estimate("2331", expected_return_unit="percent"),
        {
            **_audit_row_with_estimate("2331"),
            "expected_return_pct": 0.095,
        },
    ],
)
def test_packet_scaffold_rejects_malformed_estimate_snapshot(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    row: dict[str, object],
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path, audit_pool=[row])
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


def test_packet_scaffold_converts_huge_numeric_overflow_to_data_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    row = _audit_row_with_estimate("2331")
    row["estimate_snapshot"]["expected_return"]["annual"] = 10**400
    workspace = _prepared_workspace(tmp_path, sqlite_path, audit_pool=[row])

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


@pytest.mark.parametrize(
    ("annual", "sector_anchor", "self_anchor"),
    [
        (-1.0001, 1350.0, 1300.0),
        (10.0001, 1350.0, 1300.0),
        (0.095, 1350.0, 0.0001),
        (0.095, 1_000_000_001, None),
    ],
)
def test_packet_scaffold_rejects_values_outside_packet_estimate_contract(
    tmp_path: Path,
    annual: object,
    sector_anchor: object,
    self_anchor: object,
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    row = _audit_row_with_estimate(
        "2331",
        annual=annual,
        sector_anchor=sector_anchor,
        self_anchor=self_anchor,
    )
    workspace = _prepared_workspace(tmp_path, sqlite_path, audit_pool=[row])

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


@pytest.mark.parametrize(
    ("selection_asof", "snapshot_asof"),
    [
        ("2026-07-02", "2026-07-03"),
        ("2026-07-03", "2026-07-02"),
    ],
)
def test_prepare_rejects_selection_estimate_asof_mismatch(
    tmp_path: Path,
    selection_asof: str,
    snapshot_asof: str,
) -> None:
    selection = tmp_path / "selection.yaml"
    row = _audit_row_with_estimate("2331")
    row["estimate_snapshot"]["as_of"] = snapshot_asof
    _write_selection(selection, [row], selection_asof=selection_asof)

    code = opportunity_main(
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
        now=FIXED_NOW,
    )

    assert code == 3


def test_packet_scaffold_rejects_duplicate_audit_ticker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    workspace = _prepared_workspace(
        tmp_path,
        sqlite_path,
        audit_pool=[_audit_row("2331", 1), _audit_row("2331", 2)],
    )
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


def test_holding_packet_scaffold_rejects_raw_close_date_before_workspace_asof(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-09", 990.0, 1.0)])
    ledger = tmp_path / "ledger.yaml"
    _ledger_with_market_price_date(ledger, date(2026, 7, 10))
    workspace = tmp_path / "holding-ws"
    assert (
        opportunity_main(
            [
                "holding-prepare",
                "--asof",
                "2026-07-10",
                "--ledger",
                str(ledger),
                "--ticker",
                "2331",
                "--workspace",
                str(workspace),
            ]
        )
        == 0
    )
    capsys.readouterr()

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
        ]
    )

    assert code == 3
    assert "does not match workspace manifest as_of" in capsys.readouterr().err
    assert not (workspace / "2331").exists()


def test_packet_scaffold_draft_has_no_structural_schema_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A freshly scaffolded draft must only be flagged for unfilled operator fields,
    never for a malformed price fact — the operator fills judgment, not structure."""
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(
        sqlite_path, [("2331", "2026-07-09", 990.0, 1.0), ("2331", "2026-07-10", 1005.0, 1.0)]
    )
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    code, _ = _run(
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
    draft_path = workspace / "2331" / "packet-draft.yaml"
    findings = validate_decision_packet_file(draft_path)
    messages = " ".join(f"{finding.code} {finding.message}" for finding in findings)
    # The malformed-structure symptoms (extra price_snapshot / invalid price_basis)
    # must be absent; only unfilled judgment/metadata fields remain.
    assert "price_snapshot" not in messages
    assert "raw_unadjusted_close" not in messages
    assert "price_basis" not in messages


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
            "--db",
            str(tmp_path / "app.sqlite"),
        ],
        now=FIXED_NOW,
    )
    assert code == 3
    assert not (tmp_path / "app.sqlite").exists()


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
    db_path = tmp_path / "app.sqlite"
    code = opportunity_main(
        [
            "promote",
            "--workspace",
            str(workspace),
            "--ticker",
            "2331",
            "--db",
            str(db_path),
        ],
        now=FIXED_NOW,
    )
    assert code == 3
    assert not db_path.exists()


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
    db_path = tmp_path / "app.sqlite"
    code = opportunity_main(
        [
            "promote",
            "--workspace",
            str(workspace),
            "--ticker",
            "2331",
            "--db",
            str(db_path),
        ],
        now=FIXED_NOW,
    )
    assert code == 3
    assert not db_path.exists()


@pytest.mark.parametrize(
    ("field", "value", "error_text"),
    [
        ("ticker", "8929", "packet ticker is 8929"),
        ("as_of", "2026-07-02", "does not match workspace manifest as_of"),
    ],
)
def test_promote_rejects_packet_identity_tampering(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: str,
    error_text: str,
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    _fill_ready_workspace(workspace)
    packet_path = workspace / "2331/packet-draft.yaml"
    packet = safe_load(packet_path.read_text(encoding="utf-8"))
    packet["input_snapshot"][field] = value
    document = DecisionPacketDocument.model_validate(packet)
    packet_path.write_text(
        yaml.safe_dump(packet, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    review_path = workspace / "2331/review-draft.yaml"
    review = safe_load(review_path.read_text(encoding="utf-8"))
    review["reviewed_packet_sha256"] = decision_packet_core_hash(document)
    review_path.write_text(
        yaml.safe_dump(review, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    db_path = tmp_path / "app.sqlite"

    assert (
        opportunity_main(
            [
                "promote",
                "--workspace",
                str(workspace),
                "--ticker",
                "2331",
                "--db",
                str(db_path),
            ],
            now=FIXED_NOW,
        )
        == 3
    )
    assert error_text in capsys.readouterr().err
    assert not db_path.exists()


def test_promote_ready_publishes_atomic_packet_and_review(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    _fill_ready_workspace(workspace)
    db_path = tmp_path / "app.sqlite"

    code, _ = _run(
        [
            "promote",
            "--workspace",
            str(workspace),
            "--ticker",
            "2331",
            "--db",
            str(db_path),
        ],
        capsys,
    )
    assert code == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM research_packet").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM research_review").fetchone()[0] == 1
    packet = DecisionPacketDocument.model_validate(
        safe_load((workspace / "2331/packet-draft.yaml").read_text(encoding="utf-8"))
    )
    review = IndependentReview.model_validate(
        safe_load((workspace / "2331/review-draft.yaml").read_text(encoding="utf-8"))
    )
    assert evaluate_decision_packet(packet, review=review, now=FIXED_NOW).errors == ()


def test_screening_fv_bridge_scaffold_fill_promote_and_validate_e2e(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(
        tmp_path,
        sqlite_path,
        audit_pool=[
            _audit_row_with_estimate(
                "2331",
                sector_anchor=1507.0856,
                self_anchor=1484.5,
                annual=0.0826,
            )
        ],
    )
    code, _ = _run(
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
    _fill_ready_workspace(workspace, preserve_screening_estimate=True)
    db_path = tmp_path / "app.sqlite"

    promote_code, _ = _run(
        [
            "promote",
            "--workspace",
            str(workspace),
            "--ticker",
            "2331",
            "--db",
            str(db_path),
        ],
        capsys,
    )

    assert promote_code == 0
    promoted_review = safe_load((workspace / "2331/review-draft.yaml").read_text(encoding="utf-8"))
    assert "screening_selection" not in promoted_review["checked_source_ids"]
    packet = DecisionPacketDocument.model_validate(
        safe_load((workspace / "2331/packet-draft.yaml").read_text(encoding="utf-8"))
    )
    review = IndependentReview.model_validate(promoted_review)
    result = evaluate_decision_packet(packet, review=review, now=FIXED_NOW)
    assert result.errors == ()
    assert result.screening_fv_revision_pct is not None
    assert round(float(result.screening_fv_revision_pct), 4) == -0.1746


def test_promote_retry_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    _fill_ready_workspace(workspace)
    db_path = tmp_path / "app.sqlite"
    args = [
        "promote",
        "--workspace",
        str(workspace),
        "--ticker",
        "2331",
        "--db",
        str(db_path),
    ]
    assert opportunity_main(args, now=FIXED_NOW) == 0
    assert opportunity_main(args, now=FIXED_NOW) == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM research_packet").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM research_review").fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# plan-limit
# --------------------------------------------------------------------------- #


def _promoted_packet(tmp_path: Path, sqlite_path: Path) -> Path:
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    _fill_ready_workspace(workspace)
    db_path = tmp_path / "app.sqlite"
    assert (
        opportunity_main(
            [
                "promote",
                "--workspace",
                str(workspace),
                "--ticker",
                "2331",
                "--db",
                str(db_path),
            ],
            now=FIXED_NOW,
        )
        == 0
    )
    # plan-limit accepts an ephemeral packet file; materialize the adjacent review
    # under the ref already embedded in the draft without creating a canonical record.
    ephemeral = tmp_path / "ephemeral-packet"
    ephemeral.mkdir()
    packet_path = ephemeral / "2026-07-03-2331-decision.yaml"
    review_path = ephemeral / "2026-07-03-2331-decision-review.yaml"
    packet_path.write_text(
        (workspace / "2331/packet-draft.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    review_path.write_text(
        (workspace / "2331/review-draft.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return packet_path


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
    assert payload["price_basis"] == "last_close_unadjusted"
    assert payload["expires_at"] == "2026-07-13T15:30:00+09:00"
    assert payload["decision_packet_ref"] == str(packet)
    assert payload["decision_packet_sha256"] == hashlib.sha256(packet.read_bytes()).hexdigest()
    assert isinstance(payload["decision_packet_core_sha256"], str)
    review = packet.with_name("2026-07-03-2331-decision-review.yaml")
    assert payload["independent_review_ref"] == str(review)
    assert payload["independent_review_sha256"] == hashlib.sha256(review.read_bytes()).hexdigest()
    assert (
        payload["source_ledger_sha256"] == hashlib.sha256(LEDGER_FIXTURE.read_bytes()).hexdigest()
    )
    assert payload["portfolio_exposure"]["ledger_fallback_tickers"] == ["2331"]
    assert payload["portfolio_exposure"]["holding_valuation_status"] == (
        "mixed_with_ledger_fallback"
    )
    assert "portfolio_exposure_ledger_fallback:2331" in payload["warnings"]


def test_plan_limit_revalues_holding_on_proposal_basis_and_derives_exposure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(
        sqlite_path,
        [
            ("2331", "2026-07-09", 1100.0, 1.0),
            ("2331", "2026-07-10", 1000.0, 1.0),
        ],
    )
    packet = _promoted_packet(tmp_path, sqlite_path)
    ledger = _ledger_with_observed_at(tmp_path, "2026-07-09T15:30:00+09:00")

    code, payload = _run(
        [
            "plan-limit",
            "--packet",
            str(packet),
            "--ledger",
            str(ledger),
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
        ],
        capsys,
    )

    assert code == 0
    assert payload["status"] == "planned_limit"
    exposure = payload["portfolio_exposure"]
    # available 10,080,500 + reserved 119,000 + 200 shares * 1,000 raw close.
    assert exposure["total_capital_yen"] == 10_399_500
    assert exposure["price_as_of"] == "2026-07-10"
    assert exposure["price_basis"] == "last_close_unadjusted"
    assert exposure["holding_valuation_status"] == "same_asof_raw_close"
    assert exposure["ledger_fallback_tickers"] == []
    assert exposure["common_factor_empty_tickers"] == []
    assert exposure["ticker"] == {
        "key": "2331",
        "current_and_reserved_yen": 200_000,
        "prospective_yen": 500_000,
        "prospective_pct": 4.81,
        "warning_pct": 6,
    }
    assert exposure["sector"]["current_and_reserved_yen"] == 200_000
    assert exposure["sector"]["prospective_yen"] == 500_000
    assert exposure["common_factors"] == [
        {
            "key": "labor-automation",
            "current_and_reserved_yen": 200_000,
            "prospective_yen": 500_000,
            "prospective_pct": 4.81,
            "warning_pct": 35,
        }
    ]

    # Remaining available cash is 2,080,500. It is above 20% of the revalued
    # 10,399,500 capital (2,079,900), but below 20% of stale ledger capital
    # 10,419,500 (2,083,900); therefore no stale-denominator warning is allowed.
    code, large_order = _run(
        [
            "plan-limit",
            "--packet",
            str(packet),
            "--ledger",
            str(ledger),
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
            "--budget-max-yen",
            "8000000",
        ],
        capsys,
    )
    assert code == 0
    assert large_order["notional_yen"] == 8_000_000
    assert "dry_powder_below_floor" not in large_order["warnings"]


def test_plan_limit_active_candidate_reservation_defers_without_second_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(
        sqlite_path,
        [
            ("2331", "2026-07-09", 1100.0, 1.0),
            ("2331", "2026-07-10", 1000.0, 1.0),
        ],
    )
    packet = _promoted_packet(tmp_path, sqlite_path)
    ledger = _ledger_with_observed_at(
        tmp_path,
        "2026-07-09T15:30:00+09:00",
        active_candidate_reservation=True,
    )

    code, payload = _run(
        [
            "plan-limit",
            "--packet",
            str(packet),
            "--ledger",
            str(ledger),
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
        ],
        capsys,
    )

    assert code == 0
    assert payload["status"] == "defer"
    assert payload["close_yen"] == 1000
    assert payload["max_acceptable_price_yen"] == 1109
    assert payload["limit_price_yen"] is None
    assert payload["quantity"] == 0
    assert payload["notional_yen"] == 0
    assert payload["portfolio_annotations"] == ["already_held", "active_reservation"]
    assert payload["defer_reasons"] == ["active_reservation_exists"]
    assert "portfolio_exposure" not in payload


def test_plan_limit_counts_same_scope_reservation_and_order_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(
        sqlite_path,
        [
            ("2331", "2026-07-09", 1100.0, 1.0),
            ("2331", "2026-07-10", 1000.0, 1.0),
        ],
    )
    packet = _promoted_packet(tmp_path, sqlite_path)
    ledger = _ledger_with_observed_at(
        tmp_path,
        "2026-07-09T15:30:00+09:00",
        active_same_scope_reservation=True,
    )

    code, payload = _run(
        [
            "plan-limit",
            "--packet",
            str(packet),
            "--ledger",
            str(ledger),
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
            "--budget-max-yen",
            "400000",
        ],
        capsys,
    )

    assert code == 0
    assert payload["status"] == "planned_limit"
    assert payload["notional_yen"] == 400_000
    exposure = payload["portfolio_exposure"]
    # Reservation moves 100,000 from available to reserved, leaving capital
    # unchanged. It contributes once to the same sector/factor, while this order
    # contributes once to each prospective numerator and never to the denominator.
    assert exposure["total_capital_yen"] == 10_399_500
    assert exposure["ticker"]["current_and_reserved_yen"] == 200_000
    assert exposure["ticker"]["prospective_yen"] == 600_000
    assert exposure["sector"]["current_and_reserved_yen"] == 300_000
    assert exposure["sector"]["prospective_yen"] == 700_000
    assert exposure["common_factors"][0]["current_and_reserved_yen"] == 300_000
    assert exposure["common_factors"][0]["prospective_yen"] == 700_000
    assert payload["defer_reasons"] == []


def test_plan_limit_ticker_concentration_warning_does_not_change_status_or_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(
        sqlite_path,
        [
            ("2331", "2026-07-09", 1100.0, 1.0),
            ("2331", "2026-07-10", 1000.0, 1.0),
        ],
    )
    packet = _promoted_packet(tmp_path, sqlite_path)
    ledger = _ledger_with_observed_at(tmp_path, "2026-07-09T15:30:00+09:00")

    code, payload = _run(
        [
            "plan-limit",
            "--packet",
            str(packet),
            "--ledger",
            str(ledger),
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
            "--budget-max-yen",
            "500000",
        ],
        capsys,
    )

    assert code == 0
    assert payload["status"] == "planned_limit"
    assert payload["limit_price_yen"] == 1000
    assert payload["portfolio_exposure"]["ticker"]["prospective_yen"] == 700_000
    assert "prospective_ticker_concentration_exceeds_warning" in payload["warnings"]


def test_plan_limit_discloses_other_ticker_with_missing_common_factor_coverage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(
        sqlite_path,
        [
            ("2331", "2026-07-09", 1100.0, 1.0),
            ("2331", "2026-07-10", 1000.0, 1.0),
        ],
    )
    packet = _promoted_packet(tmp_path, sqlite_path)
    ledger = _ledger_with_observed_at(
        tmp_path,
        "2026-07-09T15:30:00+09:00",
        empty_other_reservation_factor=True,
    )

    code, payload = _run(
        [
            "plan-limit",
            "--packet",
            str(packet),
            "--ledger",
            str(ledger),
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
        ],
        capsys,
    )

    assert code == 0
    assert payload["portfolio_exposure"]["common_factor_empty_tickers"] == ["8929"]
    assert "portfolio_exposure_common_factor_coverage_incomplete" in payload["warnings"]


def test_plan_limit_falls_back_when_revalued_holding_is_not_whole_yen(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(
        sqlite_path,
        [
            ("2331", "2026-07-09", 1100.0, 1.0),
            ("2331", "2026-07-10", 1000.0025, 1.0),
        ],
    )
    packet = _promoted_packet(tmp_path, sqlite_path)
    ledger = _ledger_with_observed_at(tmp_path, "2026-07-09T15:30:00+09:00")

    code, payload = _run(
        [
            "plan-limit",
            "--packet",
            str(packet),
            "--ledger",
            str(ledger),
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
        ],
        capsys,
    )

    assert code == 0
    assert payload["status"] == "planned_limit"
    assert payload["portfolio_exposure"]["ledger_fallback_tickers"] == ["2331"]
    assert payload["portfolio_exposure"]["holding_valuation_status"] == (
        "mixed_with_ledger_fallback"
    )
    assert payload["portfolio_exposure"]["ticker"]["current_and_reserved_yen"] == 220_000
    assert "portfolio_exposure_ledger_fallback:2331" in payload["warnings"]


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


def test_plan_limit_missing_adjustment_factor_defers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, None)])
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
    assert "portfolio_exposure" not in payload


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
