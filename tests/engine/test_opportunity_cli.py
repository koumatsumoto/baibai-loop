"""Public CLI contract and golden-path tests for baibai-engine research.

These drive the CLI the way the runbook does — through scaffolds and the workspace,
never by copying a fixture thesis as the operational input. Where a fully-ready
thesis is needed (promotion), it is constructed with the public hashing API to
simulate the operator filling the draft from primary sources.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import pytest
import yaml
from pydantic import BaseModel
from tests.helpers.db_seed import seed_ledger

import baibai_engine.research.opportunity as opportunity_module
import baibai_engine.research.store as research_store_module
from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.market.sqlite.schema import open_connection
from baibai_engine.position.ledger import load_portfolio_ledger
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.research.close_source import (
    _EXPECTED_MARKET_SCHEMA_VERSION,
    resolve_holding_close_on_basis,
    resolve_previous_business_day_close,
)
from baibai_engine.research.opportunity_cli import main as opportunity_main
from baibai_engine.research.store import ResearchStoreService
from baibai_engine.research.thesis import (
    IndependentReview,
    ScreeningEstimate,
    ThesisDocument,
    ThesisIdentity,
    ThesisResult,
    UnpublishedThesis,
    evaluate_thesis,
    thesis_core_hash,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/thesis/2331-decision.yaml"
REVIEW_FIXTURE = ROOT / "tests/fixtures/thesis/2331-decision-review.yaml"
LEDGER_FIXTURE = ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"

FIXED_NOW = datetime(2026, 7, 12, 10, 0, tzinfo=JST)
TARGET_SESSION = "2026-07-13"
# The stable review filename a 2026-07-03 lane for 2331 scaffolds and promotes under.
LANE_REVIEW_NAME = "2026-07-03-2331-decision-review.yaml"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _configured_application_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BAIBAI_DB", str(tmp_path / "app.sqlite"))


def _app_db(tmp_path: Path, ledger_path: Path = LEDGER_FIXTURE) -> Path:
    name = "app.sqlite" if ledger_path == LEDGER_FIXTURE else f"app-{ledger_path.stem}.sqlite"
    path = tmp_path / name
    document = load_portfolio_ledger(ledger_path)
    document = document.model_copy(
        update={
            "market_prices": tuple(
                price.model_copy(update={"source_kind": "licensed_dataset"})
                for price in document.market_prices
            )
        }
    )
    seed_ledger(path, document)
    return path


def _assert_no_theses(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM thesis").fetchone() == (0,)


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
    source = tmp_path / "test-ledger.yaml"
    source.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return _app_db(tmp_path, source)


def _write_selection(
    path: Path,
    longlist: list[dict[str, object]],
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
        "longlist": longlist,
        "selection": selection_metadata,
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _longlist_row(ticker: str, rank: int = 1) -> dict[str, object]:
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


def _longlist_row_with_estimate(
    ticker: str,
    rank: int = 1,
    *,
    annual: object = 0.095,
    expected_return_unit: object = "annual_ratio",
    sector_anchor: object = 1350.0,
    self_anchor: object = 1300.0,
) -> dict[str, object]:
    row = _longlist_row(ticker, rank)
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
    longlist: list[dict[str, object]] | None = None,
) -> Path:
    selection = tmp_path / "selection.yaml"
    _write_selection(selection, longlist or [_longlist_row(ticker)])
    workspace = tmp_path / "ws"
    assert (
        opportunity_main(
            [
                "prepare",
                "--asof",
                "2026-07-03",
                "--selection-output",
                str(selection),
                "--db",
                str(_app_db(tmp_path)),
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


def _ledger_with_market_price_date(path: Path, observed_on: date) -> Path:
    payload = safe_load(LEDGER_FIXTURE.read_text(encoding="utf-8"))
    payload["market_prices"][0]["observed_at"] = f"{observed_on.isoformat()}T15:30:00+09:00"
    source = path.with_suffix(".yaml")
    source.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return _app_db(path.parent, source)


def _ready_thesis_and_review() -> tuple[dict[str, object], dict[str, object], str]:
    """Build an override-free ready thesis + a matching bound review from the fixture.

    All permanent-loss axes are set to acceptable/verified so no human override is
    needed; the review's reviewed_thesis_sha256 is bound to the thesis core hash.
    """
    thesis = safe_load(FIXTURE.read_text(encoding="utf-8"))
    for risk in thesis["permanent_loss_risks"]:
        risk["assessment"] = "acceptable"
        risk["evidence_status"] = "verified"
    thesis["judgment"]["permanent_loss_conclusion"] = "acceptable"
    thesis.pop("human_evidence_override", None)
    review_filename = LANE_REVIEW_NAME
    thesis["independent_review_ref"] = review_filename

    document = ThesisDocument.model_validate(thesis)
    core_hash = thesis_core_hash(document)

    review = safe_load(REVIEW_FIXTURE.read_text(encoding="utf-8"))
    review["reviewed_thesis_sha256"] = core_hash
    review["primary_source_check"] = "verified"
    return thesis, review, review_filename


def _fill_ready_workspace(
    workspace: Path,
    ticker: str = "2331",
    *,
    preserve_screening_estimate: bool = False,
) -> str:
    """Simulate the operator filling a scaffolded draft with a ready thesis+review."""
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
    thesis, review, review_filename = _ready_thesis_and_review()
    ticker_dir = workspace / ticker
    ticker_dir.mkdir(parents=True, exist_ok=True)
    if preserve_screening_estimate:
        scaffold = safe_load((ticker_dir / "thesis-draft.yaml").read_text(encoding="utf-8"))
        scaffold_snapshot = scaffold["input_snapshot"]
        screening_estimate = scaffold_snapshot["screening_estimate"]
        screening_source = next(
            source
            for source in scaffold_snapshot["sources"]
            if source["source_id"] == "screening_selection"
        )
        thesis["input_snapshot"]["sources"].append(screening_source)
        thesis["input_snapshot"]["screening_estimate"] = screening_estimate
        thesis["estimates"]["current_fair_value_yen"] = 1481.9088
        thesis["estimates"]["screening_fv_bridge"] = {
            "primary_driver": "other",
            "note": "Research kept the screening FV anchor with no material revision.",
        }
        thesis["judgment"]["proposed_at"] = FIXED_NOW.isoformat()
        review["reviewed_at"] = FIXED_NOW.isoformat()
        review["reviewed_thesis_sha256"] = thesis_core_hash(ThesisDocument.model_validate(thesis))
    (ticker_dir / "thesis-draft.yaml").write_text(
        yaml.safe_dump(thesis, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    (ticker_dir / review_filename).write_text(
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


def test_close_source_reuses_one_stable_market_snapshot(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    with sqlite3.connect(sqlite_path) as writer:
        writer.execute("PRAGMA journal_mode = WAL")

    reader = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        reader.execute("BEGIN")
        first = resolve_previous_business_day_close(
            sqlite_path=sqlite_path,
            ticker="2331",
            target_session=date(2026, 7, 13),
            connection=reader,
        )
        with sqlite3.connect(sqlite_path) as writer:
            writer.execute("UPDATE jquants_daily_bars SET close = 1200 WHERE ticker = '2331'")
        second = resolve_previous_business_day_close(
            sqlite_path=sqlite_path,
            ticker="2331",
            target_session=date(2026, 7, 13),
            connection=reader,
        )
    finally:
        reader.close()

    current = resolve_previous_business_day_close(
        sqlite_path=sqlite_path, ticker="2331", target_session=date(2026, 7, 13)
    )
    assert first is not None
    assert second is not None
    assert current is not None
    assert first.close_yen == second.close_yen == 1000.0
    assert current.close_yen == 1200.0


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
    tickers = [row["ticker"] for row in selection["longlist"]]
    assert "2331" in tickers  # annotated, never excluded
    assert selection["longlist"][0]["portfolio_annotation"] in {
        "held",
        "reserved",
        "held_and_reserved",
    }


def test_prepare_empty_longlist_is_no_actionable_bargain(
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
            "--db",
            str(_app_db(tmp_path)),
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
    ledger = _ledger_with_market_price_date(tmp_path / "ledger", date(2026, 7, 10))
    workspace = tmp_path / "holding-ws"
    code, payload = _run(
        [
            "holding-prepare",
            "--asof",
            "2026-07-10",
            "--db",
            str(ledger),
            "--ticker",
            "2331",
            "--workspace",
            str(workspace),
        ],
        capsys,
    )

    assert code == 0
    assert payload["longlist_size"] == 1
    manifest = safe_load((workspace / "manifest.yaml").read_text(encoding="utf-8"))
    selection = safe_load((workspace / "selection.yaml").read_text(encoding="utf-8"))
    comparison = safe_load((workspace / "research-comparison.yaml").read_text(encoding="utf-8"))
    assert manifest["purpose"] == "holding_review"
    assert manifest["holding_ticker"] == "2331"
    assert set(manifest["inputs"]) == {"ledger"}
    assert [row["ticker"] for row in selection["longlist"]] == ["2331"]
    assert [row["ticker"] for row in selection["shortlist"]] == ["2331"]
    assert comparison["selected_ticker"] == "2331"
    assert [row["ticker"] for row in comparison["candidates"]] == ["2331"]
    assert (
        opportunity_main(
            [
                "thesis-scaffold",
                "--workspace",
                str(workspace),
                "--db",
                str(ledger),
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
    thesis = safe_load((workspace / "2331/thesis-draft.yaml").read_text(encoding="utf-8"))
    assert thesis["input_snapshot"]["as_of"] == "2026-07-10"
    assert thesis["input_snapshot"]["sources"][0]["as_of"] == "2026-07-10"
    assert thesis["input_snapshot"]["facts"][0]["as_of"] == "2026-07-10"


@pytest.mark.parametrize("ticker", ["8929", "9999"])
def test_holding_prepare_rejects_ticker_without_open_holding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], ticker: str
) -> None:
    code = opportunity_main(
        [
            "holding-prepare",
            "--asof",
            "2026-07-11",
            "--db",
            str(_app_db(tmp_path)),
            "--ticker",
            ticker,
            "--workspace",
            str(tmp_path / "holding-ws"),
        ]
    )

    assert code == 3
    assert "not an open holding" in capsys.readouterr().err
    assert not (tmp_path / "holding-ws").exists()


def test_holding_workspace_binds_canonical_ledger_revision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = _app_db(tmp_path)
    workspace = tmp_path / "holding-ws"

    assert (
        opportunity_main(
            [
                "holding-prepare",
                "--asof",
                "2026-07-11",
                "--db",
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
    assert manifest["inputs"]["ledger"] == {
        "entity_id": "portfolio-ledger",
        "append_head": LedgerStoreService(ledger).append_head(),
    }
    assert opportunity_main(["status", "--workspace", str(workspace), "--db", str(ledger)]) == 0


def test_holding_prepare_requires_same_day_market_price(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = opportunity_main(
        [
            "holding-prepare",
            "--asof",
            "2026-07-10",
            "--db",
            str(_app_db(tmp_path)),
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
        [_longlist_row(str(1000 + index), rank=index + 1) for index in range(4)],
        research_selection_target_max=configured_max,
    )
    code, payload = _run(
        [
            "prepare",
            "--asof",
            "2026-07-03",
            "--selection-output",
            str(selection),
            "--db",
            str(_app_db(tmp_path)),
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
        [_longlist_row("2331")],
        research_selection_target_max=invalid_max,
    )
    code = opportunity_main(
        [
            "prepare",
            "--asof",
            "2026-07-03",
            "--selection-output",
            str(selection),
            "--db",
            str(_app_db(tmp_path)),
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


def test_status_waits_for_human_shortlist_before_thesis_scaffold(
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
    assert "/shortlist" in str(payload["next_command"])


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
    assert payload["next_command"] == "baibai-engine research thesis-scaffold --ticker 2331"


def test_status_waits_for_all_lane_checks_before_comparison(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    assert (
        opportunity_main(
            [
                "thesis-scaffold",
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
    thesis, _review, _review_filename = _ready_thesis_and_review()
    (workspace / "2331" / "thesis-draft.yaml").write_text(
        yaml.safe_dump(thesis, sort_keys=False, allow_unicode=True), encoding="utf-8"
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


def test_status_rejects_shortlist_ticker_outside_longlist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    selection_file = workspace / "selection.yaml"
    selection = safe_load(selection_file.read_text(encoding="utf-8"))
    selection["shortlist"] = [{"ticker": "9999", "reason": "not in longlist"}]
    selection_file.write_text(yaml.safe_dump(selection, sort_keys=False), encoding="utf-8")
    code = opportunity_main(["status", "--workspace", str(workspace)], now=FIXED_NOW)
    assert code == 3


def test_thesis_scaffold_requires_primary_research_set_membership(
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
            "thesis-scaffold",
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


def test_thesis_scaffold_confines_research_lane_to_direct_ticker_child(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    selection_output = tmp_path / "selection.yaml"
    _write_selection(selection_output, [_longlist_row("../outside")])
    workspace = tmp_path / "ws"
    assert (
        opportunity_main(
            [
                "prepare",
                "--asof",
                "2026-07-03",
                "--selection-output",
                str(selection_output),
                "--db",
                str(_app_db(tmp_path)),
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
            "thesis-scaffold",
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
    _write_selection(selection_output, [_longlist_row("2331"), _longlist_row("8929", rank=2)])
    workspace = tmp_path / "ws"
    assert (
        opportunity_main(
            [
                "prepare",
                "--asof",
                "2026-07-03",
                "--selection-output",
                str(selection_output),
                "--db",
                str(_app_db(tmp_path)),
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
                    "thesis-scaffold",
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
    assert manifest["inputs"]["ledger"] == {
        "entity_id": "portfolio-ledger",
        "append_head": LedgerStoreService(_app_db(tmp_path)).append_head(),
    }
    assert (workspace / "manifest.yaml").read_bytes() == manifest_before
    first_thesis = safe_load((workspace / "2331" / "thesis-draft.yaml").read_text("utf-8"))
    second_thesis_path = workspace / "8929" / "thesis-draft.yaml"
    second_thesis_before = second_thesis_path.read_bytes()
    assert first_thesis["input_snapshot"]["ticker"] == "2331"
    assert safe_load(second_thesis_before.decode("utf-8"))["input_snapshot"]["ticker"] == "8929"

    assert (
        opportunity_main(
            [
                "thesis-scaffold",
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
    assert second_thesis_path.read_bytes() == second_thesis_before


# --------------------------------------------------------------------------- #
# thesis-scaffold
# --------------------------------------------------------------------------- #


def test_thesis_scaffold_snapshots_raw_close(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(
        sqlite_path, [("2331", "2026-07-09", 990.0, 1.0), ("2331", "2026-07-10", 1005.0, 1.0)]
    )
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    code, payload = _run(
        [
            "thesis-scaffold",
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
    draft = safe_load((workspace / "2331" / "thesis-draft.yaml").read_text(encoding="utf-8"))
    snapshot = draft["input_snapshot"]
    # The raw close is emitted as the single schema-valid market_price fact, not a
    # bespoke price_snapshot block. price_basis uses the canonical schema enum.
    assert "price_snapshot" not in snapshot
    facts = snapshot["facts"]
    assert [item["fact_kind"] for item in facts] == ["market_price", "valuation_metric"]
    fact = facts[0]
    assert fact["value"] == 1005.0
    # JPY_per_share is what the snapshot contract requires of a market price; a bare
    # JPY unit would make the scaffold's own output fail evaluation.
    assert fact["unit"] == "JPY_per_share"
    assert fact["as_of"] == "2026-07-10"
    assert fact["observed_at"] == "2026-07-10T15:30:00+09:00"
    assert fact["price_basis"] == "last_close_unadjusted"
    # The fact references a declared local_data source.
    assert fact["source_ids"] == [snapshot["sources"][0]["source_id"]]
    # The valuation slot the snapshot contract requires is laid out with a sentinel
    # value, keyed to the fact ID the break-even check reads.
    assert facts[1] == {
        "fact_id": "trailing-per",
        "fact_kind": "valuation_metric",
        "value": "TODO",
        "unit": "ratio",
        "as_of": "2026-07-03",
        "source_ids": ["market_close"],
    }
    # The review reference is the stable filename review-scaffold writes, so no copy
    # step stands between the drafts and promotion.
    assert draft["independent_review_ref"] == LANE_REVIEW_NAME
    assert "screening_estimate" not in snapshot


def test_thesis_scaffold_transfers_raw_screening_estimate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    workspace = _prepared_workspace(
        tmp_path,
        sqlite_path,
        longlist=[_longlist_row_with_estimate("2331")],
    )
    code, payload = _run(
        [
            "thesis-scaffold",
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
    draft = safe_load((workspace / "2331" / "thesis-draft.yaml").read_text(encoding="utf-8"))
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
            "thesis-scaffold",
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
    regenerated = safe_load((workspace / "2331" / "thesis-draft.yaml").read_text(encoding="utf-8"))
    assert regenerated["input_snapshot"]["screening_estimate"] == snapshot["screening_estimate"]


def test_thesis_scaffold_reads_hash_bound_selection_not_editable_longlist_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    workspace = _prepared_workspace(
        tmp_path,
        sqlite_path,
        longlist=[_longlist_row_with_estimate("2331")],
    )
    workspace_selection_path = workspace / "selection.yaml"
    editable = safe_load(workspace_selection_path.read_text(encoding="utf-8"))
    editable_row = editable["longlist"][0]
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
            "thesis-scaffold",
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
    draft = safe_load((workspace / "2331" / "thesis-draft.yaml").read_text(encoding="utf-8"))
    estimate = draft["input_snapshot"]["screening_estimate"]
    assert estimate["expected_return_annual_ratio"] == 0.095
    assert estimate["fair_value_anchor_yen"] == 1300.0


def test_thesis_scaffold_keeps_null_fair_value_without_inventing_anchor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    workspace = _prepared_workspace(
        tmp_path,
        sqlite_path,
        longlist=[_longlist_row_with_estimate("2331", sector_anchor=None, self_anchor=None)],
    )
    code, payload = _run(
        [
            "thesis-scaffold",
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
    draft = safe_load((workspace / "2331" / "thesis-draft.yaml").read_text(encoding="utf-8"))
    assert draft["input_snapshot"]["screening_estimate"]["fair_value_anchor_yen"] is None


def test_thesis_scaffold_quantizes_screening_anchor_to_thesis_precision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    workspace = _prepared_workspace(
        tmp_path,
        sqlite_path,
        longlist=[
            _longlist_row_with_estimate("2331", sector_anchor=1350.0, self_anchor=1300.123456)
        ],
    )

    code, payload = _run(
        [
            "thesis-scaffold",
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
    draft = safe_load((workspace / "2331" / "thesis-draft.yaml").read_text(encoding="utf-8"))
    estimate = draft["input_snapshot"]["screening_estimate"]
    assert estimate["fair_value_anchor_yen"] == 1300.1235
    ScreeningEstimate.model_validate(estimate)


@pytest.mark.parametrize(
    "row",
    [
        _longlist_row_with_estimate("2331", expected_return_unit="percent"),
        {
            **_longlist_row_with_estimate("2331"),
            "expected_return_pct": 0.095,
        },
    ],
)
def test_thesis_scaffold_rejects_malformed_estimate_snapshot(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    row: dict[str, object],
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path, longlist=[row])
    code = opportunity_main(
        [
            "thesis-scaffold",
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


def test_thesis_scaffold_converts_huge_numeric_overflow_to_data_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    row = _longlist_row_with_estimate("2331")
    row["estimate_snapshot"]["expected_return"]["annual"] = 10**400
    workspace = _prepared_workspace(tmp_path, sqlite_path, longlist=[row])

    code = opportunity_main(
        [
            "thesis-scaffold",
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
def test_thesis_scaffold_rejects_values_outside_thesis_estimate_contract(
    tmp_path: Path,
    annual: object,
    sector_anchor: object,
    self_anchor: object,
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    row = _longlist_row_with_estimate(
        "2331",
        annual=annual,
        sector_anchor=sector_anchor,
        self_anchor=self_anchor,
    )
    workspace = _prepared_workspace(tmp_path, sqlite_path, longlist=[row])

    code = opportunity_main(
        [
            "thesis-scaffold",
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
    row = _longlist_row_with_estimate("2331")
    row["estimate_snapshot"]["as_of"] = snapshot_asof
    _write_selection(selection, [row], selection_asof=selection_asof)

    code = opportunity_main(
        [
            "prepare",
            "--asof",
            "2026-07-03",
            "--selection-output",
            str(selection),
            "--db",
            str(_app_db(tmp_path)),
            "--workspace",
            str(tmp_path / "ws"),
        ],
        now=FIXED_NOW,
    )

    assert code == 3


def test_thesis_scaffold_rejects_duplicate_longlist_ticker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    workspace = _prepared_workspace(
        tmp_path,
        sqlite_path,
        longlist=[_longlist_row("2331", 1), _longlist_row("2331", 2)],
    )
    code = opportunity_main(
        [
            "thesis-scaffold",
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


def test_holding_thesis_scaffold_rejects_raw_close_date_before_workspace_asof(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-09", 990.0, 1.0)])
    ledger = _ledger_with_market_price_date(tmp_path / "ledger", date(2026, 7, 10))
    workspace = tmp_path / "holding-ws"
    assert (
        opportunity_main(
            [
                "holding-prepare",
                "--asof",
                "2026-07-10",
                "--db",
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
            "thesis-scaffold",
            "--workspace",
            str(workspace),
            "--db",
            str(ledger),
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


def test_thesis_scaffold_draft_has_no_structural_schema_errors(
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
            "thesis-scaffold",
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
    draft_path = workspace / "2331" / "thesis-draft.yaml"
    draft = safe_load(draft_path.read_text(encoding="utf-8"))
    assert isinstance(draft, dict)
    snapshot = draft["input_snapshot"]
    assert isinstance(snapshot, dict)
    assert "price_snapshot" not in snapshot
    facts = snapshot["facts"]
    assert isinstance(facts, list)
    assert facts[0]["fact_kind"] == "market_price"
    assert facts[0]["price_basis"] == "last_close_unadjusted"


def test_thesis_scaffold_without_raw_close_exits_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    # Only an adjusted-only row (raw close NULL); the scaffold must not guess.
    _seed_bars(sqlite_path, [("2331", "2026-07-10", None, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    code = opportunity_main(
        [
            "thesis-scaffold",
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


def test_thesis_scaffold_defers_when_latest_bar_is_adjusted_only(
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
            "thesis-scaffold",
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


def test_thesis_scaffold_blocks_checklist_on_corporate_action(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 0.5)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    code, payload = _run(
        [
            "thesis-scaffold",
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


def _fill_scaffolded_thesis(draft: dict[str, object], fixture: dict[str, object]) -> None:
    """Fill only the slots the scaffold left blank, keeping every emitted structure.

    This is the operator's mechanical pass: identity, primary sources, the observed
    ratio, and the judgment blocks. The scaffolded market price fact, valuation slot,
    and review reference are used exactly as written.
    """
    snapshot = draft["input_snapshot"]
    fixture_snapshot = fixture["input_snapshot"]
    primary = next(
        source for source in fixture_snapshot["sources"] if source["source_id"] == "primary-results"
    )
    snapshot["company_name"] = fixture_snapshot["company_name"]
    snapshot["sector"] = fixture_snapshot["sector"]
    snapshot["common_factors"] = fixture_snapshot["common_factors"]
    snapshot["sources"].append(primary)

    trailing = next(fact for fact in snapshot["facts"] if fact["fact_id"] == "trailing-per")
    trailing["value"] = 10.32
    trailing["source_ids"] = ["market_close", "primary-results"]
    snapshot["facts"].extend(
        fact
        for fact in fixture_snapshot["facts"]
        if fact["fact_kind"] in {"net_income_attributable_to_owners", "shares_outstanding"}
    )

    metrics = fixture["derived"]["metrics"]
    for metric in metrics:
        metric["source_ids"] = ["primary-results"]
    draft["derived"] = {"metrics": metrics}

    estimates = fixture["estimates"]
    estimates["market_price_fact_id"] = "market_price_close"
    estimates["entry_price_source_ids"] = ["market_close"]
    draft["estimates"] = estimates
    draft["permanent_loss_risks"] = fixture["permanent_loss_risks"]
    judgment = fixture["judgment"]
    # The proposal is written after the close it reasons about.
    judgment["proposed_at"] = FIXED_NOW.isoformat()
    draft["judgment"] = judgment


def _fill_scaffolded_review(draft: dict[str, object], fixture: dict[str, object]) -> None:
    """Fill the reviewer's slots without touching the scaffolded thesis binding."""
    recalculated = {
        (row["horizon_years"], row["name"]): row["total_return_cagr_pct"]
        for row in fixture["recalculated_scenarios"]
    }
    for row in draft["recalculated_scenarios"]:
        row["total_return_cagr_pct"] = recalculated[(row["horizon_years"], row["name"])]
    draft["review_id"] = fixture["review_id"]
    draft["reviewer_identity"] = fixture["reviewer_identity"]
    draft["reviewer_run_id"] = fixture["reviewer_run_id"]
    draft["reviewed_at"] = FIXED_NOW.isoformat()
    draft["primary_source_check"] = "verified"
    draft["checked_source_ids"] = ["primary-results", "market_close"]
    draft["strongest_countercase"] = fixture["strongest_countercase"]
    draft["alternative_candidate_check"] = fixture["alternative_candidate_check"]


def test_scaffolded_drafts_promote_without_repairing_their_own_structure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Everything the scaffold emits survives its own evaluate / promote gate.

    Only the judgment the scaffold cannot observe is filled in; the market price
    fact and its unit, the valuation slot, the review reference, and the review file
    the scaffold wrote are all used exactly as produced. A regression here is what
    forces an operator into edit-evaluate round trips before the gate opens.
    """
    sqlite_path = tmp_path / "market.sqlite"
    # A workspace prepared for the next session resolves its close on its own as_of,
    # which is what the snapshot contract requires of the market price fact.
    _seed_bars(sqlite_path, [("2331", "2026-07-03", 1032.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    scaffold_code, _ = _run(
        [
            "thesis-scaffold",
            "--workspace",
            str(workspace),
            "--ticker",
            "2331",
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            "2026-07-06",
        ],
        capsys,
    )
    assert scaffold_code == 0

    thesis_path = workspace / "2331" / "thesis-draft.yaml"
    draft = safe_load(thesis_path.read_text(encoding="utf-8"))
    fixture, fixture_review, _ = _ready_thesis_and_review()
    _fill_scaffolded_thesis(draft, fixture)
    thesis_path.write_text(
        yaml.safe_dump(draft, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    # The review is scaffolded after the thesis is complete so its hash binds.
    review_code, review_payload = _run(
        ["review-scaffold", "--workspace", str(workspace), "--ticker", "2331"], capsys
    )
    assert review_code == 0
    review_path = workspace / "2331" / LANE_REVIEW_NAME
    assert Path(str(review_payload["review_draft"])) == review_path
    review = safe_load(review_path.read_text(encoding="utf-8"))
    _fill_scaffolded_review(review, fixture_review)
    review_path.write_text(
        yaml.safe_dump(review, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    checklist_path = workspace / "2331" / "research-checklist.yaml"
    checklist = safe_load(checklist_path.read_text(encoding="utf-8"))
    for check in checklist["checks"]:
        check["status"] = "complete"
        check["source_ids"] = ["primary-results"]
    checklist_path.write_text(
        yaml.safe_dump(checklist, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    comparison_path = workspace / "research-comparison.yaml"
    comparison = safe_load(comparison_path.read_text(encoding="utf-8"))
    comparison["selected_ticker"] = "2331"
    comparison_path.write_text(
        yaml.safe_dump(comparison, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    document = ThesisDocument.model_validate(safe_load(thesis_path.read_text(encoding="utf-8")))
    independent_review = IndependentReview.model_validate(
        safe_load(review_path.read_text(encoding="utf-8"))
    )
    assert (
        evaluate_thesis(
            document, review=independent_review, now=FIXED_NOW, identity=UnpublishedThesis.DRAFT
        ).errors
        == ()
    )

    db_path = tmp_path / "app.sqlite"
    promote_code, promote_payload = _run(
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
    assert promote_payload["review_id"] == "review-2331-20260703"
    # No copy stands beside the drafts: the scaffolded review is the promoted one.
    assert sorted(path.name for path in (workspace / "2331").glob("*.yaml")) == [
        LANE_REVIEW_NAME,
        "research-checklist.yaml",
        "thesis-draft.yaml",
    ]

    # plan-limit resolves the same adjacent review straight from the workspace draft.
    plan_code, plan_payload = _run(
        [
            "plan-limit",
            "--thesis",
            str(thesis_path),
            "--db",
            str(_app_db(tmp_path)),
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            "2026-07-06",
            "--budget-min-yen",
            "200000",
            "--budget-max-yen",
            "300000",
        ],
        capsys,
    )
    assert plan_code == 0
    assert plan_payload["independent_review_ref"] == str(review_path)
    assert "thesis_not_decision_ready" not in plan_payload["defer_reasons"]


def test_review_scaffold_header_names_the_closed_vocabulary_fields(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    _fill_ready_workspace(workspace)

    assert (
        opportunity_main(
            ["review-scaffold", "--workspace", str(workspace), "--ticker", "2331", "--force"],
            now=FIXED_NOW,
        )
        == 0
    )

    header = (workspace / "2331" / LANE_REVIEW_NAME).read_text(encoding="utf-8")
    assert "# - primary_source_check: verified | partially_verified | unverified" in header
    assert "# - alternative_candidate_check: compared | unavailable" in header


def test_enum_field_header_skips_fields_without_a_choice() -> None:
    class _Draft(BaseModel):
        verdict: Literal["pass", "fail"]
        role: Literal["only_one"]
        note: str

    header = opportunity_module._enum_field_header(_Draft)

    assert header == "# - verdict: pass | fail\n"


def test_review_scaffold_goes_stale_when_thesis_hash_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    _fill_ready_workspace(workspace)
    # Bind the review to the current thesis hash.
    assert (
        opportunity_main(
            ["review-scaffold", "--workspace", str(workspace), "--ticker", "2331", "--force"],
            now=FIXED_NOW,
        )
        == 0
    )
    # Mutate the thesis so the bound review is now stale.
    thesis_path = workspace / "2331" / "thesis-draft.yaml"
    thesis = safe_load(thesis_path.read_text(encoding="utf-8"))
    thesis["judgment"]["strongest_countercase"] = "A different countercase after re-review."
    thesis_path.write_text(yaml.safe_dump(thesis, sort_keys=False), encoding="utf-8")
    # The scaffolded (null) review is not the ready fixture review, so refill the
    # ready review bound to the OLD hash to isolate the staleness gate.
    _, review, _ = _ready_thesis_and_review()
    (workspace / "2331" / LANE_REVIEW_NAME).write_text(
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
    _assert_no_theses(tmp_path / "app.sqlite")


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
    _assert_no_theses(db_path)


def test_promote_rejects_canonical_ledger_append_head_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    _fill_ready_workspace(workspace)
    db_path = tmp_path / "app.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO ledger_event(
                append_seq, event_id, occurred_at, same_instant_order,
                event_type, ticker, proposal_id, payload
            )
            SELECT max(append_seq) + 1, 'drift-test', '2099-01-01T00:00:00+00:00',
                   0, event_type, ticker, NULL, payload
            FROM ledger_event
            """
        )

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

    assert code == 4
    assert "append head drift" in capsys.readouterr().err
    _assert_no_theses(db_path)


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
    _assert_no_theses(db_path)


@pytest.mark.parametrize(
    ("field", "value", "error_text"),
    [
        ("ticker", "8929", "thesis ticker is 8929"),
        ("as_of", "2026-07-02", "does not match workspace manifest as_of"),
    ],
)
def test_promote_rejects_thesis_identity_tampering(
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
    thesis_path = workspace / "2331/thesis-draft.yaml"
    thesis = safe_load(thesis_path.read_text(encoding="utf-8"))
    thesis["input_snapshot"][field] = value
    document = ThesisDocument.model_validate(thesis)
    thesis_path.write_text(
        yaml.safe_dump(thesis, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    review_path = workspace / "2331" / LANE_REVIEW_NAME
    review = safe_load(review_path.read_text(encoding="utf-8"))
    review["reviewed_thesis_sha256"] = thesis_core_hash(document)
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
    _assert_no_theses(db_path)


def test_promote_ready_publishes_atomic_thesis_and_review(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    _fill_ready_workspace(workspace)
    db_path = tmp_path / "app.sqlite"
    operation_instants: list[datetime] = []
    validation_instants: list[datetime | None] = []
    real_service = opportunity_module.ResearchStoreService
    real_pre_store_evaluate = opportunity_module.evaluate_thesis
    real_store_evaluate = research_store_module.evaluate_thesis

    def recording_pre_store_evaluate(
        document: ThesisDocument,
        *,
        identity: ThesisIdentity,
        review: IndependentReview | None = None,
        now: datetime | None = None,
    ) -> ThesisResult:
        validation_instants.append(now)
        return real_pre_store_evaluate(document, review=review, now=now, identity=identity)

    def recording_store_evaluate(
        document: ThesisDocument,
        *,
        review: IndependentReview | None = None,
        now: datetime | None = None,
        identity: ThesisIdentity,
    ) -> ThesisResult:
        validation_instants.append(now)
        return real_store_evaluate(document, review=review, now=now, identity=identity)

    def service_factory(
        path: Path | None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> ResearchStoreService:
        assert clock is not None

        def recording_clock() -> datetime:
            operation_now = clock()
            operation_instants.append(operation_now)
            return operation_now

        return real_service(path, clock=recording_clock)

    monkeypatch.setattr(opportunity_module, "ResearchStoreService", service_factory)
    monkeypatch.setattr(opportunity_module, "evaluate_thesis", recording_pre_store_evaluate)
    monkeypatch.setattr(research_store_module, "evaluate_thesis", recording_store_evaluate)

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
    assert operation_instants == [FIXED_NOW]
    assert validation_instants
    assert set(validation_instants) == {FIXED_NOW}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM thesis").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM thesis_review").fetchone()[0] == 1
    thesis = ThesisDocument.model_validate(
        safe_load((workspace / "2331/thesis-draft.yaml").read_text(encoding="utf-8"))
    )
    review = IndependentReview.model_validate(
        safe_load((workspace / "2331" / LANE_REVIEW_NAME).read_text(encoding="utf-8"))
    )
    assert (
        evaluate_thesis(
            thesis, review=review, now=FIXED_NOW, identity=UnpublishedThesis.DRAFT
        ).errors
        == ()
    )


def test_promote_publishes_a_researched_lane_with_no_selected_ticker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A cycle that buys nothing still produced the judgment that says why, and the
    # bargain assessment binds every lane's machine values to a stored thesis.
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    _fill_ready_workspace(workspace)
    comparison_path = workspace / "research-comparison.yaml"
    comparison = safe_load(comparison_path.read_text(encoding="utf-8"))
    comparison["selected_ticker"] = None
    comparison_path.write_text(
        yaml.safe_dump(comparison, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    db_path = tmp_path / "app.sqlite"

    code, _ = _run(
        ["promote", "--workspace", str(workspace), "--ticker", "2331", "--db", str(db_path)],
        capsys,
    )

    assert code == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM thesis").fetchone()[0] == 1


def test_promote_refuses_a_ticker_outside_the_primary_research_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    _fill_ready_workspace(workspace)
    selection_path = workspace / "selection.yaml"
    selection = safe_load(selection_path.read_text(encoding="utf-8"))
    selection["shortlist"] = []
    selection_path.write_text(
        yaml.safe_dump(selection, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    db_path = tmp_path / "app.sqlite"

    code = opportunity_main(
        ["promote", "--workspace", str(workspace), "--ticker", "2331", "--db", str(db_path)],
        now=FIXED_NOW,
    )

    assert code == 3
    _assert_no_theses(db_path)


def test_screening_fv_bridge_scaffold_fill_promote_and_validate_e2e(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(
        tmp_path,
        sqlite_path,
        longlist=[
            _longlist_row_with_estimate(
                "2331",
                sector_anchor=1507.0856,
                self_anchor=1484.5,
                annual=0.0826,
            )
        ],
    )
    code, _ = _run(
        [
            "thesis-scaffold",
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
    promoted_review = safe_load((workspace / "2331" / LANE_REVIEW_NAME).read_text(encoding="utf-8"))
    assert "screening_selection" not in promoted_review["checked_source_ids"]
    thesis = ThesisDocument.model_validate(
        safe_load((workspace / "2331/thesis-draft.yaml").read_text(encoding="utf-8"))
    )
    review = IndependentReview.model_validate(promoted_review)
    result = evaluate_thesis(thesis, review=review, now=FIXED_NOW, identity=UnpublishedThesis.DRAFT)
    assert result.errors == ()
    assert result.screening_fv_revision_pct is not None
    assert round(float(result.screening_fv_revision_pct), 4) == -0.1746


def test_promote_retry_is_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
        assert connection.execute("SELECT count(*) FROM thesis").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM thesis_review").fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# plan-limit
# --------------------------------------------------------------------------- #


def _promoted_thesis(tmp_path: Path, sqlite_path: Path) -> Path:
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
    # plan-limit accepts an ephemeral thesis file; copy the lane's thesis and its
    # adjacent review out of the workspace without creating a canonical record.
    ephemeral = tmp_path / "ephemeral-thesis"
    ephemeral.mkdir()
    thesis_path = ephemeral / "2026-07-03-2331-decision.yaml"
    review_path = ephemeral / "2026-07-03-2331-decision-review.yaml"
    thesis_path.write_text(
        (workspace / "2331/thesis-draft.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    review_path.write_text(
        (workspace / "2331" / review_path.name).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return thesis_path


def test_plan_limit_close_within_max_plans_limit_at_close(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    thesis = _promoted_thesis(tmp_path, sqlite_path)
    code, payload = _run(
        [
            "plan-limit",
            "--thesis",
            str(thesis),
            "--db",
            str(_app_db(tmp_path)),
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
    assert payload["thesis_ref"] == str(thesis)
    assert payload["thesis_sha256"] == hashlib.sha256(thesis.read_bytes()).hexdigest()
    assert isinstance(payload["thesis_core_sha256"], str)
    review = thesis.with_name("2026-07-03-2331-decision-review.yaml")
    assert payload["independent_review_ref"] == str(review)
    assert payload["independent_review_sha256"] == hashlib.sha256(review.read_bytes()).hexdigest()
    assert payload["source_ledger_entity"] == "portfolio-ledger"
    assert (
        payload["source_ledger_append_head"] == LedgerStoreService(_app_db(tmp_path)).append_head()
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
    thesis = _promoted_thesis(tmp_path, sqlite_path)
    ledger = _ledger_with_observed_at(tmp_path, "2026-07-09T15:30:00+09:00")

    code, payload = _run(
        [
            "plan-limit",
            "--thesis",
            str(thesis),
            "--db",
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
        "warning_pct": 10,
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
            "--thesis",
            str(thesis),
            "--db",
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
    thesis = _promoted_thesis(tmp_path, sqlite_path)
    ledger = _ledger_with_observed_at(
        tmp_path,
        "2026-07-09T15:30:00+09:00",
        active_candidate_reservation=True,
    )

    code, payload = _run(
        [
            "plan-limit",
            "--thesis",
            str(thesis),
            "--db",
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
    assert payload["max_acceptable_price_yen"] == 1083
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
    thesis = _promoted_thesis(tmp_path, sqlite_path)
    ledger = _ledger_with_observed_at(
        tmp_path,
        "2026-07-09T15:30:00+09:00",
        active_same_scope_reservation=True,
    )

    code, payload = _run(
        [
            "plan-limit",
            "--thesis",
            str(thesis),
            "--db",
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
    thesis = _promoted_thesis(tmp_path, sqlite_path)
    ledger = _ledger_with_observed_at(tmp_path, "2026-07-09T15:30:00+09:00")

    code, payload = _run(
        [
            "plan-limit",
            "--thesis",
            str(thesis),
            "--db",
            str(ledger),
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
            "--budget-max-yen",
            "900000",
        ],
        capsys,
    )

    assert code == 0
    assert payload["status"] == "planned_limit"
    assert payload["limit_price_yen"] == 1000
    assert payload["portfolio_exposure"]["ticker"]["prospective_yen"] == 1_100_000
    assert payload["portfolio_exposure"]["ticker"]["warning_pct"] == 10.0
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
    thesis = _promoted_thesis(tmp_path, sqlite_path)
    ledger = _ledger_with_observed_at(
        tmp_path,
        "2026-07-09T15:30:00+09:00",
        empty_other_reservation_factor=True,
    )

    code, payload = _run(
        [
            "plan-limit",
            "--thesis",
            str(thesis),
            "--db",
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
    thesis = _promoted_thesis(tmp_path, sqlite_path)
    ledger = _ledger_with_observed_at(tmp_path, "2026-07-09T15:30:00+09:00")

    code, payload = _run(
        [
            "plan-limit",
            "--thesis",
            str(thesis),
            "--db",
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
    thesis = _promoted_thesis(tmp_path, sqlite_path)
    code, payload = _run(
        [
            "plan-limit",
            "--thesis",
            str(thesis),
            "--db",
            str(_app_db(tmp_path)),
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
    thesis = _promoted_thesis(tmp_path, sqlite_path)
    code, payload = _run(
        [
            "plan-limit",
            "--thesis",
            str(thesis),
            "--db",
            str(_app_db(tmp_path)),
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
    thesis = _promoted_thesis(tmp_path, sqlite_path)
    code, payload = _run(
        [
            "plan-limit",
            "--thesis",
            str(thesis),
            "--db",
            str(_app_db(tmp_path)),
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
    thesis = _promoted_thesis(tmp_path, sqlite_path)
    code, payload = _run(
        [
            "plan-limit",
            "--thesis",
            str(thesis),
            "--db",
            str(_app_db(tmp_path)),
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
    thesis = _promoted_thesis(tmp_path, sqlite_path)
    # 1 lot = 1000 * 100 = 100,000 > budget_max 90,000.
    code, payload = _run(
        [
            "plan-limit",
            "--thesis",
            str(thesis),
            "--db",
            str(_app_db(tmp_path)),
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
    thesis = _promoted_thesis(tmp_path, sqlite_path)
    code, payload = _run(
        [
            "plan-limit",
            "--thesis",
            str(thesis),
            "--db",
            str(_app_db(tmp_path)),
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
    thesis = _promoted_thesis(tmp_path, sqlite_path)

    def run_with_budget(budget_max: str) -> dict[str, object]:
        code, payload = _run(
            [
                "plan-limit",
                "--thesis",
                str(thesis),
                "--db",
                str(_app_db(tmp_path)),
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
    thesis = _promoted_thesis(tmp_path, sqlite_path)
    args = [
        "plan-limit",
        "--thesis",
        str(thesis),
        "--db",
        str(_app_db(tmp_path)),
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
