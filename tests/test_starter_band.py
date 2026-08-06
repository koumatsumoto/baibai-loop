"""要求利回りの境界帯へ縮小 lot で入る starter の契約。

帯を開く緩和は、宣言・サイズ・bucket の 3 つが同時に効いているときだけ成立する。
どれか 1 つが外れたときに黙って full と同じ挙動へ落ちないことを確かめる。
"""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.market.sqlite.schema import SQLITE_SCHEMA_VERSION
from baibai_engine.position.ledger import PortfolioLedgerDocument, reconcile_portfolio
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.proposals.store import (
    PlannedLimitInput,
    ProposalStoreService,
    ProposalValidationError,
    _require_starter_bucket_headroom,
)
from baibai_engine.research.opportunity import plan_limit
from baibai_engine.research.store import ResearchStoreService
from baibai_engine.research.thesis import (
    IndependentReview,
    ThesisDocument,
    independent_review_hash,
    thesis_core_hash,
)
from tests.helpers.db_seed import seed_ledger
from tests.helpers.fixed_now import FIXED_NOW

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/thesis/2331-decision.yaml"
REVIEW_FIXTURE = ROOT / "tests/fixtures/thesis/2331-decision-review.yaml"
LEDGER = ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"
THESIS_ID = "thesis-20260711-2331-starter"
CREATED_AT = datetime.fromisoformat("2026-07-11T10:02:00+09:00")


def _raw() -> dict[str, object]:
    raw = safe_load(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return copy.deepcopy(raw)


def _starter(**overrides: object) -> dict[str, object]:
    raw = _raw()
    judgment = raw["judgment"]
    assert isinstance(judgment, dict)
    judgment["position_intent"] = "starter"
    judgment["sizing_action"] = "reduced"
    judgment["starter_catalyst_date"] = "2026-11-06"
    judgment.update(overrides)
    return raw


def test_a_starter_thesis_states_its_reduced_sizing_and_dated_catalyst() -> None:
    document = ThesisDocument.model_validate(_starter())

    assert document.judgment.position_intent == "starter"
    assert document.judgment.starter_catalyst_date == date(2026, 11, 6)


def test_starter_without_a_dated_catalyst_is_rejected() -> None:
    with pytest.raises(ValidationError, match="dated catalyst"):
        ThesisDocument.model_validate(_starter(starter_catalyst_date=None))


def test_starter_at_normal_sizing_is_rejected() -> None:
    with pytest.raises(ValidationError, match="reduced sizing"):
        ThesisDocument.model_validate(_starter(sizing_action="normal"))


def test_starter_with_an_elevated_permanent_loss_is_rejected() -> None:
    with pytest.raises(ValidationError, match="permanent loss"):
        ThesisDocument.model_validate(_starter(permanent_loss_conclusion="elevated"))


def test_a_full_thesis_cannot_carry_a_starter_catalyst_date() -> None:
    raw = _raw()
    judgment = raw["judgment"]
    assert isinstance(judgment, dict)
    judgment["starter_catalyst_date"] = "2026-11-06"

    with pytest.raises(ValidationError, match="starter position intent"):
        ThesisDocument.model_validate(raw)


def test_the_default_intent_leaves_the_published_hash_unchanged() -> None:
    baseline = thesis_core_hash(ThesisDocument.model_validate(_raw()))

    explicit_full = _raw()
    judgment = explicit_full["judgment"]
    assert isinstance(judgment, dict)
    judgment["position_intent"] = "full"

    assert thesis_core_hash(ThesisDocument.model_validate(explicit_full)) == baseline


def test_a_starter_declaration_changes_the_hash_and_survives_a_dump_round_trip() -> None:
    baseline = thesis_core_hash(ThesisDocument.model_validate(_raw()))
    document = ThesisDocument.model_validate(_starter())
    starter_hash = thesis_core_hash(document)

    assert starter_hash != baseline
    dumped = document.model_dump(mode="json")
    assert thesis_core_hash(ThesisDocument.model_validate(dumped)) == starter_hash


def _write_starter_workspace(directory: Path, **overrides: object) -> Path:
    """starter thesis と、その hash へ束縛し直した独立レビューを隣接ファイルとして書く。"""

    raw = _starter(**overrides)
    thesis_path = directory / "starter-decision.yaml"
    _rebind(raw, directory)
    thesis_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return thesis_path


def _rebind(raw: dict[str, object], directory: Path) -> None:
    """thesis を書き換えたら、review と human override の hash 束縛を張り直す。

    束縛が切れたままだと「証拠不足の buy に override が要る」で落ちるだけで、
    starter の contract を試したことにならない。
    """

    document = ThesisDocument.model_validate(raw)
    core_hash = thesis_core_hash(document)
    review = safe_load(REVIEW_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(review, dict)
    review["reviewed_thesis_sha256"] = core_hash
    review_path = directory / str(document.independent_review_ref)
    review_path.write_text(yaml.safe_dump(review, allow_unicode=True), encoding="utf-8")
    override = raw.get("human_evidence_override")
    if isinstance(override, dict):
        override["proposal_sha256"] = core_hash
        override["review_sha256"] = independent_review_hash(
            IndependentReview.model_validate(review)
        )


def _market(path: Path, *, close: float) -> Path:
    market = path.with_name("market.sqlite")
    with sqlite3.connect(market) as connection:
        connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS jquants_daily_bars "
            "(ticker TEXT, traded_at TEXT, close REAL, adjustment_factor REAL)"
        )
        connection.execute("DELETE FROM jquants_daily_bars")
        connection.execute(
            "INSERT INTO jquants_daily_bars VALUES ('2331', '2026-07-10', ?, 1)", (close,)
        )
    return market


def _seed_app(path: Path, thesis_path: Path) -> None:
    raw = safe_load(thesis_path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    document = ThesisDocument.model_validate(raw)
    review_path = thesis_path.with_name(str(document.independent_review_ref))
    review = safe_load(review_path.read_text(encoding="utf-8"))
    assert isinstance(review, dict)
    ResearchStoreService(path, clock=lambda: FIXED_NOW).publish_thesis_with_review(
        THESIS_ID, raw, review
    )
    ledger = PortfolioLedgerDocument.model_validate(safe_load(LEDGER.read_text(encoding="utf-8")))
    seed_ledger(
        path,
        ledger.model_copy(
            update={
                "market_prices": tuple(
                    price.model_copy(update={"source_kind": "licensed_dataset"})
                    for price in ledger.market_prices
                )
            }
        ),
    )


def test_starter_sizing_stops_at_the_notional_cap_instead_of_filling_the_budget(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.sqlite"
    thesis_path = _write_starter_workspace(tmp_path)
    market = _market(path, close=1000)
    _seed_app(path, thesis_path)

    planned = plan_limit(
        thesis=thesis_path,
        db_path=path,
        sqlite_path=market,
        target_session=date(2026, 7, 13),
        budget_min_yen=200_000,
        budget_max_yen=300_000,
        now=CREATED_AT,
    )

    # full なら 300,000 円まで 3 単元を埋める。starter は 100,000 円で止まる。
    assert planned["status"] == "planned_limit"
    assert planned["quantity"] == 100
    assert planned["notional_yen"] == 100_000


def test_starter_defers_when_a_single_lot_already_exceeds_the_notional_cap(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.sqlite"
    thesis_path = _write_starter_workspace(tmp_path)
    market = _market(path, close=1050)
    _seed_app(path, thesis_path)

    planned = plan_limit(
        thesis=thesis_path,
        db_path=path,
        sqlite_path=market,
        target_session=date(2026, 7, 13),
        budget_min_yen=200_000,
        budget_max_yen=300_000,
        now=CREATED_AT,
    )

    assert planned["status"] == "defer"
    assert planned["defer_reasons"] == ["starter_lot_exceeds_notional_cap"]


def test_a_starter_proposal_outside_the_opened_band_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    thesis_path = _write_starter_workspace(tmp_path)
    raw = safe_load(thesis_path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    estimates["required_5y_base_cagr_pct"] = 8.5
    _rebind(raw, tmp_path)
    thesis_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    market = _market(path, close=1000)
    _seed_app(path, thesis_path)
    planned = plan_limit(
        thesis=thesis_path,
        db_path=path,
        sqlite_path=market,
        target_session=date(2026, 7, 13),
        budget_min_yen=200_000,
        budget_max_yen=300_000,
        now=CREATED_AT,
    )
    service = ProposalStoreService(
        path, market_db_path=market, clock=lambda: CREATED_AT + timedelta(hours=1)
    )

    with pytest.raises(ProposalValidationError, match="starter proposal requires"):
        service.create(
            THESIS_ID,
            _planned_input(planned),
            reconcile_portfolio(LedgerStoreService(path).load()),
            snapshot_append_head=LedgerStoreService(path).append_head(),
            created_at=CREATED_AT,
        )


def test_the_starter_bucket_counts_only_capital_deployed_through_starter_proposals(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.sqlite"
    thesis_path = _write_starter_workspace(tmp_path)
    _seed_app(path, thesis_path)
    snapshot = reconcile_portfolio(LedgerStoreService(path).load())
    ceiling = snapshot.total_capital_yen * 10 // 100
    _record_execution(path, proposal_id="prop-starter", intent="starter", notional_yen=ceiling)
    _record_execution(path, proposal_id="prop-full", intent="full", notional_yen=ceiling)

    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        # full 分は数えないので、bucket は ceiling ちょうど。1 円足すと超える。
        _require_starter_bucket_headroom(connection, snapshot=snapshot, planned_notional_yen=0)
        with pytest.raises(ProposalValidationError, match="starter bucket"):
            _require_starter_bucket_headroom(connection, snapshot=snapshot, planned_notional_yen=1)


def _record_execution(path: Path, *, proposal_id: str, intent: str, notional_yen: int) -> None:
    """proposal を経由した執行を 1 件置く。ledger の replay ではなく join だけを試す。"""

    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO proposal (proposal_id, ticker, thesis_id, review_id, created_at, "
            "status, decided_at, payload) VALUES (?, '9999', ?, ?, ?, 'approved', ?, ?)",
            (
                proposal_id,
                THESIS_ID,
                "review-2331-20260703",
                "2026-06-01T10:00:00+09:00",
                "2026-06-01T11:00:00+09:00",
                json.dumps({"position_intent": intent}),
            ),
        )
        connection.execute(
            "INSERT INTO ledger_event (event_id, occurred_at, same_instant_order, event_type, "
            "ticker, proposal_id, payload) VALUES (?, ?, ?, 'execution', '9999', ?, ?)",
            (
                f"execution-{proposal_id}",
                "2026-06-02T10:00:00+09:00",
                0 if intent == "starter" else 1,
                proposal_id,
                json.dumps({"quantity": notional_yen, "price_yen": "1"}),
            ),
        )


def _planned_input(planned: dict[str, object]) -> PlannedLimitInput:
    return PlannedLimitInput.model_validate(planned)
