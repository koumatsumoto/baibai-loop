"""Public CLI contract and golden-path tests for baibai-engine research.

These drive the CLI the way the runbook does — through scaffolds and the workspace,
never by copying a fixture thesis as the operational input. Where a fully-ready
thesis is needed (promotion), it is constructed with the public hashing API to
simulate the operator filling the draft from primary sources.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import pytest
import yaml
from pydantic import BaseModel
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger
from tests.helpers.research_gate import research_gate_shortlist, seed_shortlist
from tests.helpers.screening_sqlite import seed_daily_bars

import baibai_engine.research.opportunity as opportunity_module
import baibai_engine.research.store as research_store_module
from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.store import LedgerStoreService
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

SELECTION_ID = "selection-opportunity-test"
RUN_REVISION_ID = "run-revision-opportunity-test"
SHORTLIST_ID = "shortlist-20260703-opportunity-test"

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
    ranked_set: list[dict[str, object]],
    *,
    research_selection_target_max: object = 5,
    selection_asof: str | None = "2026-07-03",
    screening_rules_hash: str | None = None,
    er_model_version: str | None = None,
    selection_id: str = SELECTION_ID,
    run_revision_id: str = RUN_REVISION_ID,
) -> None:
    selection_metadata: dict[str, object] = {
        "research_selection_target_max": research_selection_target_max,
        "evidence_pattern_order": ["cashflow-yield-discount"],
        "input_refs": {"candidates_ref": run_revision_id},
    }
    if selection_asof is not None:
        selection_metadata["asof"] = selection_asof
    if screening_rules_hash is not None:
        selection_metadata["screening_rules_hash"] = screening_rules_hash
    if er_model_version is not None:
        selection_metadata["er_model_version"] = er_model_version
    payload = {
        "selection_id": selection_id,
        "ranked_set": ranked_set,
        "ranked_tickers": [str(row["ticker"]) for row in ranked_set],
        "selection": selection_metadata,
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _seed_gate(
    tmp_path: Path,
    selection_path: Path,
    *,
    rejected: Sequence[str] = (),
    as_of: str = "2026-07-03",
    selection_id: str | None = None,
    run_revision_id: str | None = None,
    shortlist_id: str = SHORTLIST_ID,
    ledger_path: Path = LEDGER_FIXTURE,
) -> Path:
    """Publish the Research Gate judgment over the ranked set this selection carries.

    Reading the tickers back out of the selection keeps one source for the cycle: a
    shortlist that judged a different set is exactly what prepare must refuse, so a
    test asks for that by overriding, never by drifting.
    """

    selection = safe_load(selection_path.read_text(encoding="utf-8"))
    ranked_tickers = [str(ticker) for ticker in selection.get("ranked_tickers") or []]
    excluded = set(rejected)
    db_path = _app_db(tmp_path, ledger_path)
    seed_shortlist(
        db_path,
        research_gate_shortlist(
            shortlist_id=shortlist_id,
            selection_id=selection_id or str(selection["selection_id"]),
            run_revision_id=(
                run_revision_id or str(selection["selection"]["input_refs"]["candidates_ref"])
            ),
            as_of=as_of,
            selected=[ticker for ticker in ranked_tickers if ticker not in excluded],
            rejected=[ticker for ticker in ranked_tickers if ticker in excluded],
        ),
    )
    return db_path


def _ranked_set_row(ticker: str, rank: int = 1) -> dict[str, object]:
    return {
        "rank": rank,
        "ticker": ticker,
        "name": f"candidate {ticker}",
        "primary_evidence_pattern_id": "cashflow-yield-discount",
        "expected_return_pct": 9.5,
        "fair_value_anchor_yen": 1300,
        "market_price_yen": 1000,
        "liquidity_status": "pass",
        "durability_warnings": [],
        "event_warnings": [],
        "selection_reasons": ["cashflow-yield-discount"],
    }


def _ranked_set_row_with_estimate(
    ticker: str,
    rank: int = 1,
    *,
    annual: object = 0.095,
    expected_return_unit: object = "annual_ratio",
    sector_anchor: object = 1350.0,
    self_anchor: object = 1300.0,
) -> dict[str, object]:
    row = _ranked_set_row(ticker, rank)
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
    ranked_set: list[dict[str, object]] | None = None,
) -> Path:
    selection = tmp_path / "selection.yaml"
    _write_selection(selection, ranked_set or [_ranked_set_row(ticker)])
    workspace = tmp_path / "ws"
    assert (
        opportunity_main(
            [
                "prepare",
                "--asof",
                "2026-07-03",
                "--selection-output",
                str(selection),
                "--shortlist-id",
                SHORTLIST_ID,
                "--db",
                str(_seed_gate(tmp_path, selection)),
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


def _rewrite_holding_workspace(workspace: Path, *, ticker: str, asof: str) -> None:
    """Point a holding-review workspace at another subject and as-of, by hand.

    All three documents move together because that is the shape of the hole: a
    manifest alone trips the draft as_of check first, which is not the same gate.
    """

    for name, key in (("selection.yaml", "ranked_set"), ("research-comparison.yaml", "candidates")):
        path = workspace / name
        document = safe_load(path.read_text(encoding="utf-8"))
        document["as_of"] = asof
        document[key] = [{**row, "ticker": ticker} for row in document[key]]
        if key == "ranked_set":
            document["shortlist"] = [{**row, "ticker": ticker} for row in document["shortlist"]]
        else:
            document["selected_ticker"] = ticker
        path.write_text(
            yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
    manifest_path = workspace / "manifest.yaml"
    manifest = safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["holding_ticker"] = ticker
    manifest["as_of"] = asof
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def _move_market_price_day(db_path: Path, *, ticker: str, observed_at: str) -> None:
    """Re-apply one holding's market price, the way a new price draft does."""

    service = LedgerStoreService(db_path)
    document, head = service.load_with_head()
    service.apply_document(
        expected_head=head,
        expected_document=document,
        replacement=document.model_copy(
            update={
                # A price draft moves the document's as_of with the observation; the
                # ledger refuses a price observed after it. Neither is an event.
                "as_of": datetime.fromisoformat(observed_at),
                "market_prices": tuple(
                    price.model_copy(update={"observed_at": datetime.fromisoformat(observed_at)})
                    if price.ticker == ticker
                    else price
                    for price in document.market_prices
                ),
            }
        ),
    )


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
    thesis["judgment"]["sizing_action"] = "normal"
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


def test_review_scaffold_help_requires_a_stable_thesis(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        opportunity_main(["review-scaffold", "--help"])

    assert error.value.code == 0
    assert "after the thesis content is stable" in capsys.readouterr().out


def test_prepare_rejects_selection_output_inside_generated_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    selection = workspace / "source-selection.yaml"
    _write_selection(selection, [_ranked_set_row("2331")])
    original = selection.read_text(encoding="utf-8")

    code, _payload = _run(
        [
            "prepare",
            "--asof",
            "2026-07-03",
            "--selection-output",
            str(selection),
            "--shortlist-id",
            SHORTLIST_ID,
            "--db",
            str(_seed_gate(tmp_path, selection)),
            "--workspace",
            str(workspace),
        ],
        capsys,
    )

    assert code == 3
    assert selection.read_text(encoding="utf-8") == original
    assert not (workspace / "manifest.yaml").exists()


def test_prepare_annotates_held_reserved_without_excluding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    # 2331 is a holding in the representative ledger.
    workspace = _prepared_workspace(tmp_path, sqlite_path, ticker="2331")

    selection = safe_load((workspace / "selection.yaml").read_text(encoding="utf-8"))
    tickers = [row["ticker"] for row in selection["ranked_set"]]
    assert "2331" in tickers  # annotated, never excluded
    assert selection["ranked_set"][0]["portfolio_annotation"] in {
        "held",
        "reserved",
        "held_and_reserved",
    }


def test_prepare_selecting_nothing_is_no_actionable_bargain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A Gate that rejected everything is a finished cycle, not a broken one.

    The workspace still exists — the comparison context is the record of what was
    looked at — but nothing can be admitted, so no thesis can start.
    """
    selection = tmp_path / "selection.yaml"
    _write_selection(selection, [_ranked_set_row("2331"), _ranked_set_row("8929", rank=2)])
    workspace = tmp_path / "ws"
    code, payload = _run(
        [
            "prepare",
            "--asof",
            "2026-07-03",
            "--selection-output",
            str(selection),
            "--shortlist-id",
            SHORTLIST_ID,
            "--db",
            str(_seed_gate(tmp_path, selection, rejected=["2331", "8929"])),
            "--workspace",
            str(workspace),
        ],
        capsys,
    )
    assert code == 0
    assert payload["actionable"] is False
    assert payload["admissible_tickers"] == []
    assert payload["shortlist_slots"] == 0
    assert payload["note"] == "no actionable bargain"

    workspace_selection = safe_load((workspace / "selection.yaml").read_text(encoding="utf-8"))
    # Rejected candidates stay as comparison context and carry the Gate's answer.
    assert [row["ticker"] for row in workspace_selection["ranked_set"]] == ["2331", "8929"]
    assert {row["research_gate_decision"] for row in workspace_selection["ranked_set"]} == {
        "rejected"
    }

    code = opportunity_main(
        [
            "thesis-scaffold",
            "--workspace",
            str(workspace),
            "--db",
            str(_app_db(tmp_path)),
            "--ticker",
            "2331",
            "--sqlite-path",
            str(tmp_path / "market.sqlite"),
            "--target-session",
            TARGET_SESSION,
        ]
    )
    assert code == 3
    assert "not in the primary-research set" in capsys.readouterr().err
    assert not (workspace / "2331").exists()


def test_prepare_rejects_selection_without_ranked_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Research cannot start when the source selection has no ranked set."""
    selection = tmp_path / "selection.yaml"
    _write_selection(selection, [])
    code = opportunity_main(
        [
            "prepare",
            "--asof",
            "2026-07-03",
            "--selection-output",
            str(selection),
            "--shortlist-id",
            SHORTLIST_ID,
            "--db",
            str(_app_db(tmp_path)),
            "--workspace",
            str(tmp_path / "ws"),
        ]
    )
    assert code == 3
    assert "source selection has no ranked set" in capsys.readouterr().err
    assert not (tmp_path / "ws").exists()


def test_holding_prepare_builds_fixed_one_ticker_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
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
    assert payload["ranked_set_size"] == 1
    manifest = safe_load((workspace / "manifest.yaml").read_text(encoding="utf-8"))
    selection = safe_load((workspace / "selection.yaml").read_text(encoding="utf-8"))
    comparison = safe_load((workspace / "research-comparison.yaml").read_text(encoding="utf-8"))
    assert manifest["purpose"] == "holding_review"
    assert manifest["holding_ticker"] == "2331"
    assert set(manifest["inputs"]) == {"ledger"}
    assert [row["ticker"] for row in selection["ranked_set"]] == ["2331"]
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


# What makes a legitimate holding-review subject. `holding-prepare` proves it once
# and every later gate re-proves the same set, so the two are driven from one table:
# a precondition only one of them holds is how `purpose: holding_review` turns into a
# way around the Research Gate.
HOLDING_SUBJECT_REJECTIONS = (
    pytest.param("8929", "2026-07-11", "not an open holding", id="held_by_no_one"),
    pytest.param("9999", "2026-07-11", "not an open holding", id="unknown_ticker"),
    pytest.param("2331", "2026-07-10", "market price on 2026-07-11", id="wrong_as_of"),
)


@pytest.mark.parametrize(("ticker", "asof", "expected"), HOLDING_SUBJECT_REJECTIONS)
def test_holding_prepare_rejects_an_illegitimate_subject(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ticker: str,
    asof: str,
    expected: str,
) -> None:
    code = opportunity_main(
        [
            "holding-prepare",
            "--asof",
            asof,
            "--db",
            str(_app_db(tmp_path)),
            "--ticker",
            ticker,
            "--workspace",
            str(tmp_path / "holding-ws"),
        ]
    )

    assert code == 3
    assert expected in capsys.readouterr().err
    assert not (tmp_path / "holding-ws").exists()


@pytest.mark.parametrize(("ticker", "asof", "expected"), HOLDING_SUBJECT_REJECTIONS)
def test_every_gate_re_proves_the_holding_subject_prepare_proved(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ticker: str,
    asof: str,
    expected: str,
) -> None:
    """A manifest claiming what `holding-prepare` would refuse gets no further.

    The manifest is a rebuildable file, so each rejection `holding-prepare` makes
    has to survive being written into one by hand.
    """
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [(ticker, "2026-07-10", 1000.0, 1.0)])
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

    _rewrite_holding_workspace(workspace, ticker=ticker, asof=asof)

    # Every gate, not just the cheapest one: they share one verification, and a
    # workspace that reached `promote` would already have spent the research.
    for argv in (
        ["status", "--workspace", str(workspace), "--db", str(ledger)],
        [
            "thesis-scaffold",
            "--workspace",
            str(workspace),
            "--db",
            str(ledger),
            "--ticker",
            ticker,
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
        ],
        ["review-scaffold", "--workspace", str(workspace), "--db", str(ledger), "--ticker", ticker],
        ["promote", "--workspace", str(workspace), "--db", str(ledger), "--ticker", ticker],
    ):
        assert opportunity_main(argv, now=FIXED_NOW) == 4, argv[0]
        error = capsys.readouterr().err
        assert "holding-review workspace subject is invalid" in error, argv[0]
        assert expected in error, argv[0]
    assert not (workspace / ticker).exists()


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


def test_a_holding_workspace_stops_when_the_ledger_price_moves_under_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A re-applied price draft invalidates an in-flight review, so the gate says so.

    ``append_head`` cannot carry this: it counts ledger events, while market prices
    are replaced in their own table, so the observation date moves under an
    unchanged head. The canonical holding review is built against that observation
    (`holding_review_builder`), so the workspace is already unusable — the gate only
    decides whether the operator learns it now or after writing the research.
    """
    ledger_path = tmp_path / "moving-price-ledger.yaml"
    payload = safe_load(LEDGER_FIXTURE.read_text(encoding="utf-8"))
    payload["market_prices"][0]["source_kind"] = "licensed_dataset"
    ledger_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    db_path = _app_db(tmp_path, ledger_path)
    workspace = tmp_path / "holding-ws"
    assert (
        opportunity_main(
            [
                "holding-prepare",
                "--asof",
                "2026-07-11",
                "--db",
                str(db_path),
                "--ticker",
                "2331",
                "--workspace",
                str(workspace),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert opportunity_main(["status", "--workspace", str(workspace), "--db", str(db_path)]) == 0
    capsys.readouterr()

    head_before = LedgerStoreService(db_path).append_head()
    _move_market_price_day(db_path, ticker="2331", observed_at="2026-07-12T15:30:00+09:00")
    # The pin the workspace holds is untouched by a price-only write, which is why
    # the subject has to be re-proved rather than assumed fresh.
    assert LedgerStoreService(db_path).append_head() == head_before

    code = opportunity_main(["status", "--workspace", str(workspace), "--db", str(db_path)])

    assert code == 4
    error = capsys.readouterr().err
    assert "market price on 2026-07-12, not 2026-07-11" in error
    # Market prices only move forward, so the old as-of cannot be restored: the
    # recovery the message names has to be the one that actually works.
    assert "holding-prepare --asof <observed> --force" in error
    assert "thesis-scaffold --force" in error


def test_rebuilding_a_holding_workspace_reports_the_draft_left_at_the_old_as_of(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Re-preparing at a new as-of leaves the written research behind, and status says so.

    `--force` rewrites the workspace documents but not `<ws>/<ticker>/`, so a thesis
    the operator already finished survives at the previous as-of. Without this the
    rebuilt workspace calls itself ready_for_review and sends them to write an
    independent review that promote could never accept.
    """
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    ledger_path = tmp_path / "moving-price-ledger.yaml"
    payload = safe_load(LEDGER_FIXTURE.read_text(encoding="utf-8"))
    payload["market_prices"][0]["source_kind"] = "licensed_dataset"
    payload["market_prices"][0]["observed_at"] = "2026-07-10T15:30:00+09:00"
    ledger_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    db_path = _app_db(tmp_path, ledger_path)
    workspace = tmp_path / "holding-ws"

    def prepare(asof: str, *, force: bool = False) -> int:
        return opportunity_main(
            [
                "holding-prepare",
                "--asof",
                asof,
                "--db",
                str(db_path),
                "--ticker",
                "2331",
                "--workspace",
                str(workspace),
                *(["--force"] if force else []),
            ]
        )

    assert prepare("2026-07-10") == 0
    assert (
        opportunity_main(
            [
                "thesis-scaffold",
                "--workspace",
                str(workspace),
                "--db",
                str(db_path),
                "--ticker",
                "2331",
                "--sqlite-path",
                str(sqlite_path),
                "--target-session",
                "2026-07-11",
            ],
            now=FIXED_NOW,
        )
        == 0
    )
    capsys.readouterr()
    # Research the operator already finished, carrying the as-of it was written for.
    thesis, _review, _review_name = _ready_thesis_and_review()
    (workspace / "2331" / "thesis-draft.yaml").write_text(
        yaml.safe_dump(thesis, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    code, payload_out = _run(
        ["status", "--workspace", str(workspace), "--db", str(db_path)], capsys
    )
    assert code == 0
    assert payload_out["thesis_validation_errors"] == [
        (
            "2331:thesis as_of 2026-07-03 does not match workspace as_of 2026-07-10; "
            "regenerate it with `research thesis-scaffold --force`"
        )
    ]

    _move_market_price_day(db_path, ticker="2331", observed_at="2026-07-11T15:30:00+09:00")
    assert prepare("2026-07-11", force=True) == 0
    capsys.readouterr()

    code, payload_out = _run(
        ["status", "--workspace", str(workspace), "--db", str(db_path)], capsys
    )

    assert code == 0
    assert payload_out["workspace_status"] == "incomplete"
    # The rebuild moved the workspace, not the ticker directory: the finished
    # research is still there, still unusable, and now says so before the review.
    assert payload_out["thesis_validation_errors"] == [
        (
            "2331:thesis as_of 2026-07-03 does not match workspace as_of 2026-07-11; "
            "regenerate it with `research thesis-scaffold --force`"
        )
    ]


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
        [_ranked_set_row(str(1000 + index), rank=index + 1) for index in range(4)],
        research_selection_target_max=configured_max,
    )
    code, payload = _run(
        [
            "prepare",
            "--asof",
            "2026-07-03",
            "--selection-output",
            str(selection),
            "--shortlist-id",
            SHORTLIST_ID,
            "--db",
            str(_seed_gate(tmp_path, selection)),
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


def test_prepare_binds_matching_er_distribution_context(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = tmp_path / "selection.yaml"
    _write_selection(
        selection,
        [_ranked_set_row("2331")],
        screening_rules_hash="rules-hash",
        er_model_version="expected-return-v1",
    )
    context_path = tmp_path / "er-context.yaml"
    stats = {
        "median": 0.18,
        "q25": 0.09,
        "q10": 0.01,
        "trap_rate": 0.12,
        "n": 900,
    }
    bases = [
        {
            "basis": basis,
            "ticker_equal": stats,
            "cohort_equal": stats,
        }
        for basis in ("fy_actual_dividend_total_return", "price_return_only")
    ]
    bounds = [
        ("q1", 1, None, -0.02),
        ("q2", 2, -0.02, 0.00),
        ("q3", 3, 0.00, 0.02),
        ("q4", 4, 0.02, 0.04),
        ("q5", 5, 0.04, None),
        ("er_gte_8_5pct", None, 0.085, None),
    ]
    horizons = []
    for horizon, cohort_count in (("3y", 34), ("5y", 18)):
        horizons.append(
            {
                "horizon": horizon,
                "asof_start": "2020-01-31",
                "asof_end": "2023-06-30",
                "cohort_count": cohort_count,
                "bands": [
                    {
                        "band_id": band_id,
                        "quintile": quintile,
                        "lower_er_annual": lower,
                        "upper_er_annual": upper,
                        "median_predicted_er_annual": 0.06,
                        "cohort_count": cohort_count,
                        "median_n": 230,
                        "bases": bases,
                    }
                    for band_id, quintile, lower, upper in bounds
                ],
            }
        )
    common_horizons = [
        {
            **horizon,
            "cohort_count": 18,
            "bands": [
                {**band, "cohort_count": 18} for band in horizon["bands"] if isinstance(band, dict)
            ],
        }
        for horizon in horizons
    ]
    context_path.write_text(
        yaml.safe_dump(
            {
                "kind": "er-level-calibration-context",
                "schema_version": 2,
                "generated_at": "2026-07-03T12:00:00+09:00",
                "valid_through": "2026-08-17",
                "reference_horizon": "3y",
                "screening_rules_hash": "rules-hash",
                "er_model_version": "expected-return-v1",
                "primary_realized_basis": "fy_actual_dividend_total_return",
                "secondary_realized_basis": "price_return_only",
                "trap_basis": "cohort_population_cumulative_return_excess_lte_minus_0_20",
                "weighting": {
                    "primary": "ticker_asof_observation_equal",
                    "secondary": "cohort_equal",
                },
                "common_window": {
                    "asof_start": "2020-01-31",
                    "asof_end": "2023-06-30",
                    "cohort_count": 18,
                    "horizons": common_horizons,
                },
                "horizons": horizons,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(opportunity_module, "ER_LEVEL_CALIBRATION_CONTEXT_PATH", context_path)
    workspace = tmp_path / "ws"

    code, _ = _run(
        [
            "prepare",
            "--asof",
            "2026-07-03",
            "--selection-output",
            str(selection),
            "--shortlist-id",
            SHORTLIST_ID,
            "--db",
            str(_seed_gate(tmp_path, selection)),
            "--workspace",
            str(workspace),
        ],
        capsys,
    )

    assert code == 0
    comparison = safe_load((workspace / "research-comparison.yaml").read_text(encoding="utf-8"))
    candidate_context = comparison["candidates"][0]["er_realized_distribution_context"]
    assert [band["band_id"] for band in candidate_context["horizons"][0]["bands"]] == [
        "q5",
        "er_gte_8_5pct",
    ]
    manifest = safe_load((workspace / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["inputs"]["er_distribution_context"]["path"] == str(context_path)


def test_prepare_degrades_when_optional_er_context_is_malformed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = tmp_path / "selection.yaml"
    _write_selection(
        selection,
        [_ranked_set_row("2331")],
        screening_rules_hash="rules-hash",
        er_model_version="expected-return-v1",
    )
    context_path = tmp_path / "er-context.yaml"
    context_path.write_text("[", encoding="utf-8")
    monkeypatch.setattr(opportunity_module, "ER_LEVEL_CALIBRATION_CONTEXT_PATH", context_path)
    workspace = tmp_path / "ws"

    code, _ = _run(
        [
            "prepare",
            "--asof",
            "2026-07-03",
            "--selection-output",
            str(selection),
            "--shortlist-id",
            SHORTLIST_ID,
            "--db",
            str(_seed_gate(tmp_path, selection)),
            "--workspace",
            str(workspace),
        ],
        capsys,
    )

    assert code == 0
    comparison = safe_load((workspace / "research-comparison.yaml").read_text(encoding="utf-8"))
    assert comparison["er_realized_distribution_context"] is None
    manifest = safe_load((workspace / "manifest.yaml").read_text(encoding="utf-8"))
    assert "er_distribution_context" not in manifest["inputs"]


@pytest.mark.parametrize("invalid_max", [None, -1, True, "5"])
def test_prepare_rejects_invalid_research_selection_target_max(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    invalid_max: object,
) -> None:
    selection = tmp_path / "selection.yaml"
    _write_selection(
        selection,
        [_ranked_set_row("2331")],
        research_selection_target_max=invalid_max,
    )
    code = opportunity_main(
        [
            "prepare",
            "--asof",
            "2026-07-03",
            "--selection-output",
            str(selection),
            "--shortlist-id",
            SHORTLIST_ID,
            "--db",
            str(_seed_gate(tmp_path, selection)),
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
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


def test_status_points_to_first_missing_primary_research_ticker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
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
    assert payload["next_command"] == "complete primary research for 2331"

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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    manifest = safe_load((workspace / "manifest.yaml").read_text(encoding="utf-8"))
    selection_file = Path(manifest["inputs"]["selection_output"]["path"])
    selection_file.write_text(
        selection_file.read_text(encoding="utf-8") + "\n# changed after prepare\n",
        encoding="utf-8",
    )
    code = opportunity_main(["status", "--workspace", str(workspace)], now=FIXED_NOW)
    assert code == 4


def test_status_rejects_shortlist_ticker_outside_ranked_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    selection_file = workspace / "selection.yaml"
    selection = safe_load(selection_file.read_text(encoding="utf-8"))
    selection["shortlist"] = [{"ticker": "9999", "reason": "not in ranked_set"}]
    selection_file.write_text(yaml.safe_dump(selection, sort_keys=False), encoding="utf-8")
    code = opportunity_main(["status", "--workspace", str(workspace)], now=FIXED_NOW)
    assert code == 3


# --------------------------------------------------------------------------- #
# Research Gate binding
# --------------------------------------------------------------------------- #


def _gated_workspace(
    tmp_path: Path,
    sqlite_path: Path,
    *,
    rejected: Sequence[str] = ("8929",),
) -> tuple[Path, Path, Path]:
    """Prepare a two-ticker workspace whose Gate selected some and rejected others."""

    selection = tmp_path / "selection.yaml"
    _write_selection(selection, [_ranked_set_row("2331"), _ranked_set_row("8929", rank=2)])
    db_path = _seed_gate(tmp_path, selection, rejected=rejected)
    workspace = tmp_path / "ws"
    assert (
        opportunity_main(
            [
                "prepare",
                "--asof",
                "2026-07-03",
                "--selection-output",
                str(selection),
                "--shortlist-id",
                SHORTLIST_ID,
                "--db",
                str(db_path),
                "--workspace",
                str(workspace),
            ]
        )
        == 0
    )
    return workspace, selection, db_path


def _set_primary_research_set(workspace: Path, tickers: Sequence[str]) -> None:
    selection_path = workspace / "selection.yaml"
    selection = safe_load(selection_path.read_text(encoding="utf-8"))
    selection["shortlist"] = [{"ticker": ticker, "reason": "research"} for ticker in tickers]
    selection_path.write_text(
        yaml.safe_dump(selection, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def test_research_gate_rejected_ticker_cannot_enter_the_primary_research_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ranked-set member rejected by the Gate is refused before any thesis.

    Bargain Assessment re-checks this at publication, but workspace admission must
    reject it before research creates a capital-allocation judgment.
    """
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("8929", "2026-07-10", 750.0, 1.0)])
    workspace, _selection, db_path = _gated_workspace(tmp_path, sqlite_path)
    _set_primary_research_set(workspace, ["8929"])

    code = opportunity_main(["status", "--workspace", str(workspace), "--db", str(db_path)])
    error = capsys.readouterr().err
    assert code == 3
    assert "8929" in error
    assert f"{SHORTLIST_ID} rejected at the Research Gate" in error

    code = opportunity_main(
        [
            "thesis-scaffold",
            "--workspace",
            str(workspace),
            "--db",
            str(db_path),
            "--ticker",
            "8929",
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
        ],
        now=FIXED_NOW,
    )
    assert code == 3
    assert "rejected at the Research Gate" in capsys.readouterr().err
    assert not (workspace / "8929").exists()
    _assert_no_theses(db_path)


def test_prepare_rejects_a_shortlist_that_judged_another_selection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Negative 2: a judgment over some other selection cannot bound this one.

    The judgment is looked up by the selection it judged, so one made elsewhere is
    not merely mismatched — for this cycle it does not exist.
    """
    selection = tmp_path / "selection.yaml"
    _write_selection(selection, [_ranked_set_row("2331")])
    db_path = _seed_gate(tmp_path, selection, selection_id="selection-somewhere-else")
    workspace = tmp_path / "ws"

    code = opportunity_main(
        [
            "prepare",
            "--asof",
            "2026-07-03",
            "--selection-output",
            str(selection),
            "--shortlist-id",
            SHORTLIST_ID,
            "--db",
            str(db_path),
            "--workspace",
            str(workspace),
        ]
    )
    error = capsys.readouterr().err
    assert code == 3
    assert f"no canonical Research Gate judgment for selection {SELECTION_ID}" in error
    assert not workspace.exists()


@pytest.mark.parametrize(
    ("gate_as_of", "gate_run_revision_id", "expected"),
    [
        ("2026-07-02", None, "must both equal 2026-07-03"),
        (None, "run-revision-other", "judged run 'run-revision-other'"),
    ],
)
def test_prepare_rejects_a_shortlist_from_another_cycle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    gate_as_of: str | None,
    gate_run_revision_id: str | None,
    expected: str,
) -> None:
    """Negative 3: same selection ID is not enough — as-of and run must agree too."""
    selection = tmp_path / "selection.yaml"
    _write_selection(selection, [_ranked_set_row("2331")])
    db_path = _seed_gate(
        tmp_path,
        selection,
        as_of=gate_as_of or "2026-07-03",
        run_revision_id=gate_run_revision_id,
    )
    workspace = tmp_path / "ws"

    code = opportunity_main(
        [
            "prepare",
            "--asof",
            "2026-07-03",
            "--selection-output",
            str(selection),
            "--shortlist-id",
            SHORTLIST_ID,
            "--db",
            str(db_path),
            "--workspace",
            str(workspace),
        ]
    )
    assert code == 3
    assert expected in capsys.readouterr().err
    assert not workspace.exists()


def test_prepare_rejects_a_shortlist_over_a_different_ranked_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The judgment has to cover this cycle's ranked set, member for member."""
    selection = tmp_path / "selection.yaml"
    _write_selection(selection, [_ranked_set_row("2331"), _ranked_set_row("8929", rank=2)])
    db_path = _app_db(tmp_path)
    seed_shortlist(
        db_path,
        research_gate_shortlist(
            shortlist_id=SHORTLIST_ID,
            selection_id=SELECTION_ID,
            run_revision_id=RUN_REVISION_ID,
            as_of="2026-07-03",
            selected=["2331"],
        ),
    )
    workspace = tmp_path / "ws"

    code = opportunity_main(
        [
            "prepare",
            "--asof",
            "2026-07-03",
            "--selection-output",
            str(selection),
            "--shortlist-id",
            SHORTLIST_ID,
            "--db",
            str(db_path),
            "--workspace",
            str(workspace),
        ]
    )
    error = capsys.readouterr().err
    assert code == 3
    assert "must equal the selection ranked set" in error
    assert "missing=['8929']" in error
    assert not workspace.exists()


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"schema_version": 4}, "unsupported shortlist schema_version"),
        ({"research_gate_contract_id": "research-gate-v2"}, "unsupported Research Gate contract"),
    ],
)
def test_prepare_rejects_a_shortlist_without_a_supported_research_gate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutation: dict[str, object],
    expected: str,
) -> None:
    """Negative 4: only the current judgment contract may bound research."""
    selection = tmp_path / "selection.yaml"
    _write_selection(selection, [_ranked_set_row("2331")])
    payload = research_gate_shortlist(
        shortlist_id=SHORTLIST_ID,
        selection_id=SELECTION_ID,
        run_revision_id=RUN_REVISION_ID,
        as_of="2026-07-03",
        selected=["2331"],
    )
    payload.update(mutation)
    db_path = _app_db(tmp_path)
    seed_shortlist(db_path, payload)
    workspace = tmp_path / "ws"

    code = opportunity_main(
        [
            "prepare",
            "--asof",
            "2026-07-03",
            "--selection-output",
            str(selection),
            "--shortlist-id",
            SHORTLIST_ID,
            "--db",
            str(db_path),
            "--workspace",
            str(workspace),
        ]
    )
    assert code == 3
    assert expected in capsys.readouterr().err
    assert not workspace.exists()


def test_prepare_rejects_an_unknown_shortlist_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    selection = tmp_path / "selection.yaml"
    _write_selection(selection, [_ranked_set_row("2331")])
    _seed_gate(tmp_path, selection)
    workspace = tmp_path / "ws"

    code = opportunity_main(
        [
            "prepare",
            "--asof",
            "2026-07-03",
            "--selection-output",
            str(selection),
            "--shortlist-id",
            "shortlist-20260703-absent",
            "--db",
            str(_app_db(tmp_path)),
            "--workspace",
            str(workspace),
        ]
    )
    assert code == 3
    assert f"was judged by {SHORTLIST_ID}, not shortlist-20260703-absent" in capsys.readouterr().err
    assert not workspace.exists()


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda binding: binding["selected_tickers"].append("8929"),
            id="widened_ticker_list",
        ),
        pytest.param(
            lambda binding: binding.update({"selection_id": "selection-forged"}),
            id="forged_selection_id",
        ),
    ],
)
def test_hand_edited_manifest_cannot_widen_the_admitted_set(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutate: Callable[[dict[str, object]], object],
) -> None:
    """Negative 5: the manifest records the binding; the stored shortlist is its authority."""
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("8929", "2026-07-10", 750.0, 1.0)])
    workspace, _selection, db_path = _gated_workspace(tmp_path, sqlite_path)

    manifest_path = workspace / "manifest.yaml"
    manifest = safe_load(manifest_path.read_text(encoding="utf-8"))
    mutate(manifest["inputs"]["shortlist"])
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    _set_primary_research_set(workspace, ["8929"])

    code = opportunity_main(
        [
            "thesis-scaffold",
            "--workspace",
            str(workspace),
            "--db",
            str(db_path),
            "--ticker",
            "8929",
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
        ],
        now=FIXED_NOW,
    )
    assert code == 4
    assert "does not match the canonical shortlist" in capsys.readouterr().err
    assert not (workspace / "8929").exists()


def test_repointing_the_manifest_at_another_judgment_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Naming a different real judgment must not re-bind an existing workspace.

    A published judgment that selected the rejected ticker is the strongest form of
    this: the manifest would then name a genuine Gate decision, so only anchoring
    the lookup to the workspace's own hash-pinned selection can tell the two apart.
    """
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("8929", "2026-07-10", 750.0, 1.0)])
    workspace, selection, db_path = _gated_workspace(tmp_path, sqlite_path)
    other = "shortlist-20260703-second-judgment"
    _seed_gate(tmp_path, selection, rejected=["2331"], shortlist_id=other)

    manifest_path = workspace / "manifest.yaml"
    manifest = safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["inputs"]["shortlist"]["shortlist_id"] = other
    manifest["inputs"]["shortlist"]["selected_tickers"] = ["8929"]
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    _set_primary_research_set(workspace, ["8929"])

    code = opportunity_main(["status", "--workspace", str(workspace), "--db", str(db_path)])
    error = capsys.readouterr().err
    assert code == 4
    assert "does not match the canonical shortlist" in error
    assert not (workspace / "8929").exists()


def test_prepare_admits_the_selected_subset_and_records_every_gate_decision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Negative 6: selected tickers are researchable, and slots bound the admitted set."""
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace, _selection, db_path = _gated_workspace(tmp_path, sqlite_path)

    selection = safe_load((workspace / "selection.yaml").read_text(encoding="utf-8"))
    assert selection["research_gate_shortlist_id"] == SHORTLIST_ID
    assert selection["admissible_tickers"] == ["2331"]
    assert selection["shortlist_slots"] == 1
    assert {row["ticker"]: row["research_gate_decision"] for row in selection["ranked_set"]} == {
        "2331": "selected",
        "8929": "rejected",
    }

    _set_primary_research_set(workspace, ["2331"])
    code, payload = _run(["status", "--workspace", str(workspace), "--db", str(db_path)], capsys)
    assert code == 0
    assert payload["research_gate"] == {
        "purpose": "opportunity",
        "shortlist_id": SHORTLIST_ID,
        "admissible_tickers": ["2331"],
    }

    assert (
        opportunity_main(
            [
                "thesis-scaffold",
                "--workspace",
                str(workspace),
                "--db",
                str(db_path),
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
    assert (workspace / "2331" / "thesis-draft.yaml").is_file()


def test_holding_review_workspace_needs_no_research_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Negative 8: the ledger is holding review's source, so no Gate bounds it."""
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
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

    code, payload = _run(["status", "--workspace", str(workspace), "--db", str(ledger)], capsys)
    assert code == 0
    assert payload["research_gate"] == {
        "purpose": "holding_review",
        "shortlist_id": None,
        "admissible_tickers": [],
    }

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
                "2026-07-13",
            ],
            now=FIXED_NOW,
        )
        == 0
    )
    assert (workspace / "2331" / "thesis-draft.yaml").is_file()


def test_declaring_holding_review_does_not_opt_a_workspace_out_of_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Forging the manifest's purpose must not turn the Gate off.

    Holding review has no Research Gate because the ledger is its source, so a
    workspace that claims that purpose has to prove its subject against the ledger.
    Otherwise `purpose` is simply the switch that disables the binding.
    """
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("8929", "2026-07-10", 750.0, 1.0)])
    workspace, _selection, db_path = _gated_workspace(tmp_path, sqlite_path)

    manifest_path = workspace / "manifest.yaml"
    manifest = safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["purpose"] = "holding_review"
    manifest["holding_ticker"] = "8929"
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    selection_path = workspace / "selection.yaml"
    selection = safe_load(selection_path.read_text(encoding="utf-8"))
    selection["ranked_set"] = [
        {"rank": 1, "ticker": "8929", "sector": "サービス業", "portfolio_annotation": "held"}
    ]
    selection["shortlist"] = [{"ticker": "8929", "reason": "open holding review"}]
    selection["shortlist_slots"] = 1
    selection["actionable"] = True
    selection_path.write_text(
        yaml.safe_dump(selection, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    comparison_path = workspace / "research-comparison.yaml"
    comparison = safe_load(comparison_path.read_text(encoding="utf-8"))
    comparison["candidates"] = [{"ticker": "8929"}]
    comparison["selected_ticker"] = "8929"
    comparison_path.write_text(
        yaml.safe_dump(comparison, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    code = opportunity_main(
        [
            "thesis-scaffold",
            "--workspace",
            str(workspace),
            "--db",
            str(db_path),
            "--ticker",
            "8929",
            "--sqlite-path",
            str(sqlite_path),
            "--target-session",
            TARGET_SESSION,
        ],
        now=FIXED_NOW,
    )
    assert code == 4
    assert "is not an open holding in the canonical ledger" in capsys.readouterr().err
    assert not (workspace / "8929").exists()


def test_workspace_prepared_before_the_binding_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A workspace with no Gate binding is rebuilt, never trusted."""
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace, _selection, db_path = _gated_workspace(tmp_path, sqlite_path)
    manifest_path = workspace / "manifest.yaml"
    manifest = safe_load(manifest_path.read_text(encoding="utf-8"))
    del manifest["inputs"]["shortlist"]
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    code = opportunity_main(["status", "--workspace", str(workspace), "--db", str(db_path)])
    error = capsys.readouterr().err
    assert code == 3
    assert "no Research Gate binding" in error
    assert "--shortlist-id" in error


def test_thesis_scaffold_requires_primary_research_set_membership(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
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


def test_prepare_rejects_noncanonical_ranked_set_ticker_before_workspace_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    selection_output = tmp_path / "selection.yaml"
    _write_selection(selection_output, [_ranked_set_row("../outside")])
    workspace = tmp_path / "ws"
    code = opportunity_main(
        [
            "prepare",
            "--asof",
            "2026-07-03",
            "--selection-output",
            str(selection_output),
            "--shortlist-id",
            SHORTLIST_ID,
            "--db",
            str(_app_db(tmp_path)),
            "--workspace",
            str(workspace),
        ]
    )

    assert code == 3
    assert "ranked set is invalid" in capsys.readouterr().err.lower()
    assert not workspace.exists()
    assert not (tmp_path / "outside").exists()


def test_primary_research_tickers_share_lineage_and_remain_isolated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(
        sqlite_path,
        [("2331", "2026-07-10", 1000.0, 1.0), ("8929", "2026-07-10", 750.0, 1.0)],
    )
    selection_output = tmp_path / "selection.yaml"
    _write_selection(selection_output, [_ranked_set_row("2331"), _ranked_set_row("8929", rank=2)])
    workspace = tmp_path / "ws"
    assert (
        opportunity_main(
            [
                "prepare",
                "--asof",
                "2026-07-03",
                "--selection-output",
                str(selection_output),
                "--shortlist-id",
                SHORTLIST_ID,
                "--db",
                str(_seed_gate(tmp_path, selection_output)),
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
    seed_daily_bars(
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    workspace = _prepared_workspace(
        tmp_path,
        sqlite_path,
        ranked_set=[_ranked_set_row_with_estimate("2331")],
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


def test_thesis_scaffold_reads_hash_bound_selection_not_editable_ranked_set_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    workspace = _prepared_workspace(
        tmp_path,
        sqlite_path,
        ranked_set=[_ranked_set_row_with_estimate("2331")],
    )
    workspace_selection_path = workspace / "selection.yaml"
    editable = safe_load(workspace_selection_path.read_text(encoding="utf-8"))
    editable_row = editable["ranked_set"][0]
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    workspace = _prepared_workspace(
        tmp_path,
        sqlite_path,
        ranked_set=[_ranked_set_row_with_estimate("2331", sector_anchor=None, self_anchor=None)],
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    workspace = _prepared_workspace(
        tmp_path,
        sqlite_path,
        ranked_set=[
            _ranked_set_row_with_estimate("2331", sector_anchor=1350.0, self_anchor=1300.123456)
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
        _ranked_set_row_with_estimate("2331", expected_return_unit="percent"),
        {
            **_ranked_set_row_with_estimate("2331"),
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path, ranked_set=[row])
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    row = _ranked_set_row_with_estimate("2331")
    row["estimate_snapshot"]["expected_return"]["annual"] = 10**400
    workspace = _prepared_workspace(tmp_path, sqlite_path, ranked_set=[row])

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


def test_thesis_scaffold_turns_a_contract_violating_estimate_into_a_data_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Which values the thesis contract admits is `test_thesis.py`'s; that a rejection
    reaches the operator as exit 3 rather than a traceback is this boundary's.

    The scaffold carries the selection's estimate into a thesis draft, so a value the
    contract refuses arrives here as a `ValidationError` from a model the CLI does not
    own. Without the translation the command ends in a stack trace and writes nothing,
    which reads as a broken tool rather than as a run that has to be redone.
    """

    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1005.0, 1.0)])
    # Above the contract's ceiling for an annual expected return.
    row = _ranked_set_row_with_estimate("2331", annual=10.0001)
    workspace = _prepared_workspace(tmp_path, sqlite_path, ranked_set=[row])

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
    assert "screening estimate violates thesis contract" in capsys.readouterr().err


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
    row = _ranked_set_row_with_estimate("2331")
    row["estimate_snapshot"]["as_of"] = snapshot_asof
    _write_selection(selection, [row], selection_asof=selection_asof)

    code = opportunity_main(
        [
            "prepare",
            "--asof",
            "2026-07-03",
            "--selection-output",
            str(selection),
            "--shortlist-id",
            SHORTLIST_ID,
            "--db",
            str(_app_db(tmp_path)),
            "--workspace",
            str(tmp_path / "ws"),
        ],
        now=FIXED_NOW,
    )

    assert code == 3


def test_prepare_rejects_duplicate_ranked_set_ticker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    selection = tmp_path / "selection.yaml"
    _write_selection(
        selection,
        [_ranked_set_row("2331", 1), _ranked_set_row("2331", 2)],
    )
    code = opportunity_main(
        [
            "prepare",
            "--asof",
            "2026-07-03",
            "--selection-output",
            str(selection),
            "--shortlist-id",
            SHORTLIST_ID,
            "--db",
            str(_app_db(tmp_path)),
            "--workspace",
            str(tmp_path / "ws"),
        ]
    )
    assert code == 3


def test_holding_thesis_scaffold_rejects_raw_close_date_before_workspace_asof(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("2331", "2026-07-09", 990.0, 1.0)])
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


def test_thesis_scaffold_without_raw_close_exits_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    # Only an adjusted-only row (raw close NULL); the scaffold must not guess.
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", None, 1.0)])
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
    seed_daily_bars(
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 0.5)])
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-03", 1032.0, 1.0)])
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(tmp_path, sqlite_path)
    _fill_ready_workspace(workspace)
    db_path = tmp_path / "app.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO ledger_event(
                append_seq, event_id, occurred_at, same_instant_order,
                event_type, ticker, decision_reference, payload
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
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

    # A retry after an interrupted operation exits 0 and adds nothing: the store's
    # own idempotence (test_research_store.py) reaching the operator through the CLI.
    assert (
        opportunity_main(
            ["promote", "--workspace", str(workspace), "--ticker", "2331", "--db", str(db_path)],
            now=FIXED_NOW,
        )
        == 0
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM thesis").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM thesis_review").fetchone()[0] == 1


def test_promote_publishes_a_researched_lane_with_no_selected_ticker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A cycle that buys nothing still produced the judgment that says why, and the
    # bargain assessment binds every lane's machine values to a stored thesis.
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    workspace = _prepared_workspace(
        tmp_path,
        sqlite_path,
        ranked_set=[
            _ranked_set_row_with_estimate(
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
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
    seed_daily_bars(
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
    seed_daily_bars(
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
    seed_daily_bars(
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
    seed_daily_bars(
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
    seed_daily_bars(
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
    seed_daily_bars(
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 5000.0, 1.0)])
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 0.0, 1.0)])
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 0.5)])
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, None)])
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 500.005, 1.0)])
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
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
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
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
