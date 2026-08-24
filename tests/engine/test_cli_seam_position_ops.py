"""Seam coverage for the writer commands a human types by hand.

Every test here drives ``main([...])`` with a real argv list. The layer under
test is the one between argparse and the store: ``--approved-at`` /
``--expires-at`` / ``--as-of`` arrive as text and reach the model as
``datetime`` or ``date``, and ``--estimated-exit-tax-rate-bps`` reaches it as
``int``. A test that constructs those values itself never crosses that layer, so
it stays green while the command rejects every input the runbook asks for.
"""

from __future__ import annotations

import copy
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from tests.helpers.db_seed import seed_ledger
from tests.helpers.fixed_now import FIXED_NOW
from tests.helpers.ledger import load_portfolio_ledger

from baibai_engine.cli import main as engine_main
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.market.sqlite.schema import SQLITE_SCHEMA_VERSION
from baibai_engine.operation.cli import main as operation_main
from baibai_engine.position.cli import main as position_main
from baibai_engine.position.ledger import reconcile_portfolio
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.proposals.cli import main as proposal_main
from baibai_engine.proposals.store import PlannedLimitInput, ProposalStoreService
from baibai_engine.research.opportunity import plan_limit
from baibai_engine.research.store import ResearchStoreService
from baibai_engine.tasks.cli import main as task_main

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"
THESIS = ROOT / "tests/fixtures/thesis/2331-decision.yaml"
REVIEW = ROOT / "tests/fixtures/thesis/2331-decision-review.yaml"
THESIS_ID = "thesis-20260711-2331-r1"
CREATED_AT = datetime.fromisoformat("2026-07-11T10:02:00+09:00")
DECIDED_AT = CREATED_AT + timedelta(hours=1)
OPERATION_NOW = datetime.fromisoformat("2026-07-19T12:00:00+09:00")


def _ledger_db(tmp_path: Path) -> Path:
    db = tmp_path / "app.sqlite"
    source = load_portfolio_ledger(LEDGER)
    seed_ledger(
        db,
        source.model_copy(
            update={
                "market_prices": tuple(
                    price.model_copy(update={"source_kind": "licensed_dataset"})
                    for price in source.market_prices
                )
            }
        ),
    )
    return db


def _mapping(path: Path) -> dict[str, object]:
    parsed = safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return copy.deepcopy(parsed)


def _emitted(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    parsed = yaml.safe_load(capsys.readouterr().out)
    assert isinstance(parsed, dict)
    return parsed


def test_override_draft_cli_writes_the_typed_approval_window(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _ledger_db(tmp_path)
    out = tmp_path / "override-draft.yaml"

    code = position_main(
        [
            "override-draft",
            "--override-id",
            "override-20260710-2331",
            "--scope",
            "ticker",
            "--key",
            "2331",
            "--reason",
            "reviewed concentration is intentional",
            "--decision-reference",
            "holding-review-20260710-2331",
            "--approved-at",
            "2026-07-10T09:00:00+09:00",
            "--expires-at",
            "2026-08-05T09:00:00+09:00",
            "--db",
            str(db),
            "--out",
            str(out),
        ]
    )

    assert code == 0
    assert capsys.readouterr().out.startswith("status: draft_created")
    draft = safe_load(out.read_text(encoding="utf-8"))
    assert isinstance(draft, dict)
    assert draft["kind"] == "override"
    assert draft["source"]["overrides"] == []
    overrides = draft["replacement"]["overrides"]
    assert [item["override_id"] for item in overrides] == ["override-20260710-2331"]
    # The approval window is what argparse converted and the model re-parsed; a
    # command that handed the model raw text or a naive instant cannot land here.
    assert overrides[0]["approved_at"] == "2026-07-10T09:00:00+09:00"
    assert overrides[0]["expires_at"] == "2026-08-05T09:00:00+09:00"


def test_meta_draft_cli_writes_the_exit_tax_settings_as_scalars(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _ledger_db(tmp_path)
    out = tmp_path / "meta-draft.yaml"

    code = position_main(
        [
            "meta-draft",
            "--as-of",
            "2026-07-11T15:30:00+09:00",
            "--estimated-exit-tax-rate-bps",
            "2032",
            "--estimated-exit-tax-basis",
            "ledger_fifo_gross_unrealized_gain",
            "--db",
            str(db),
            "--out",
            str(out),
        ]
    )

    assert code == 0
    assert capsys.readouterr().out.startswith("status: draft_created")
    draft = safe_load(out.read_text(encoding="utf-8"))
    assert isinstance(draft, dict)
    assert draft["kind"] == "meta"
    assert draft["source"]["estimated_exit_tax_rate_bps"] is None
    replacement = draft["replacement"]
    # The rate has to reach the ledger as a number the bounds can be checked
    # against; the text "2032" is neither below 10000 bps nor multipliable.
    assert replacement["estimated_exit_tax_rate_bps"] == 2032
    assert replacement["estimated_exit_tax_basis"] == "ledger_fifo_gross_unrealized_gain"
    assert replacement["as_of"] == "2026-07-11T15:30:00+09:00"


def test_operation_checkpoint_cli_replaces_the_active_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "app.sqlite"
    assert (
        operation_main(
            ["--db", str(db), "start", "--kind", "opportunity", "--as-of", "2026-07-19"],
            now=OPERATION_NOW,
        )
        == 0
    )
    operation_id = _emitted(capsys)["operation_id"]
    payload = tmp_path / "checkpoint.yaml"
    payload.write_text(
        yaml.safe_dump(
            {
                "checkpoint": "shortlist reviewed",
                "next": "wait for the primary research set",
            }
        ),
        encoding="utf-8",
    )

    code = operation_main(
        ["--db", str(db), "checkpoint", str(operation_id), "--payload", str(payload)],
        now=OPERATION_NOW,
    )

    assert code == 0
    emitted = _emitted(capsys)
    assert emitted["payload"]["checkpoint"] == "shortlist reviewed"
    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT status, json_extract(payload, '$.checkpoint') "
            "FROM operation_session WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
    assert tuple(row) == ("active", "shortlist reviewed")


def _pending_proposal(tmp_path: Path) -> tuple[Path, Path, str]:
    """Seed one pending proposal the way the store's own tests build it."""

    ResearchStoreService(
        tmp_path / "app.sqlite", clock=lambda: FIXED_NOW
    ).publish_thesis_with_review(THESIS_ID, _mapping(THESIS), _mapping(REVIEW))
    db = _ledger_db(tmp_path)
    market = tmp_path / "market.sqlite"
    with sqlite3.connect(market) as connection:
        connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")
        connection.execute(
            "CREATE TABLE jquants_daily_bars "
            "(ticker TEXT, traded_at TEXT, close REAL, adjustment_factor REAL)"
        )
        connection.execute("INSERT INTO jquants_daily_bars VALUES ('2331', '2026-07-10', 1000, 1)")
    planned = PlannedLimitInput.model_validate(
        plan_limit(
            thesis=THESIS,
            db_path=db,
            sqlite_path=market,
            target_session=date(2026, 7, 13),
            budget_min_yen=200_000,
            budget_max_yen=300_000,
            now=CREATED_AT,
        )
    )
    ledger = LedgerStoreService(db)
    proposal = ProposalStoreService(
        db, market_db_path=market, clock=lambda: CREATED_AT + timedelta(minutes=30)
    ).create(
        THESIS_ID,
        planned,
        reconcile_portfolio(ledger.load()),
        snapshot_append_head=ledger.append_head(),
        created_at=CREATED_AT,
    )
    return db, market, proposal.proposal_id


def test_proposal_decide_cli_approves_against_the_current_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db, market, proposal_id = _pending_proposal(tmp_path)

    code = proposal_main(
        [
            "--db",
            str(db),
            "--market-db",
            str(market),
            "decide",
            proposal_id,
            "--decision",
            "approve",
        ],
        now=DECIDED_AT,
    )

    assert code == 0
    emitted = _emitted(capsys)
    assert emitted["status"] == "approved"
    # The command reconciles the ledger itself for an approval; the stored row is
    # the only proof that path ran rather than the pending row being echoed back.
    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT status, decided_at FROM proposal WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
    assert tuple(row) == ("approved", DECIDED_AT.isoformat())


def test_task_drop_cli_closes_the_row_on_the_operation_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "app.sqlite"
    today = date(2026, 7, 19)
    assert (
        task_main(
            [
                "--db",
                str(db),
                "add",
                "--title",
                "運用点検",
                "--kind",
                "ops",
                "--due",
                "2026-07-31",
            ],
            today=today,
        )
        == 0
    )
    task_id = _emitted(capsys)["task_id"]

    code = task_main(["--db", str(db), "drop", str(task_id)], today=today)

    assert code == 0
    assert _emitted(capsys)["status"] == "dropped"
    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT status, closed_at FROM task WHERE task_id = ?", (task_id,)
        ).fetchone()
    assert tuple(row) == ("dropped", "2026-07-19")


def test_db_init_cli_creates_the_application_database_at_the_current_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "app" / "baibai.sqlite"

    code = engine_main(["db", "init", "--db", str(db)])

    assert code == 0
    reported = _emitted(capsys)
    assert reported["path"] == str(db)
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == reported["user_version"]
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_schema WHERE type = 'table' AND name = 'ledger_event'"
            ).fetchone()[0]
            == 1
        )


def test_db_backup_cli_writes_a_snapshot_beside_the_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "app" / "baibai.sqlite"
    assert engine_main(["db", "init", "--db", str(db)]) == 0
    initialized = _emitted(capsys)

    code = engine_main(["db", "backup", "--db", str(db)])

    assert code == 0
    reported = _emitted(capsys)
    assert reported["status"] == "ok"
    snapshots = sorted((db.parent / "backups").glob("baibai-*.sqlite"))
    assert [str(item) for item in snapshots] == [reported["path"]]
    with sqlite3.connect(snapshots[0]) as connection:
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0] == initialized["user_version"]
        )
