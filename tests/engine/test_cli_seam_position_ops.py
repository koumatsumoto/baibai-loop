"""Seam coverage for the writer commands a human types by hand.

Every test here drives ``main([...])`` with a real argv list. The layer under
test is the one between argparse and the store: ``--ordered-at`` /
``--expires-at`` / ``--as-of`` arrive as text and reach the model as
``datetime`` or ``date``, and ``--estimated-exit-tax-rate-bps`` reaches it as
``int``. A test that constructs those values itself never crosses that layer, so
it stays green while the command rejects every input the runbook asks for.
"""

from __future__ import annotations

import copy
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger

from baibai_engine.cli import main as engine_main
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.operation.cli import main as operation_main
from baibai_engine.position.cli import main as position_main
from baibai_engine.tasks.cli import main as task_main

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"
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
            "position-review-20260710-2331",
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
            ["--db", str(db), "start", "--kind", "position-review", "--as-of", "2026-07-19"],
            now=OPERATION_NOW,
        )
        == 0
    )
    operation_id = _emitted(capsys)["operation_id"]
    payload = tmp_path / "checkpoint.yaml"
    payload.write_text(
        yaml.safe_dump(
            {
                "checkpoint": "research_triage reviewed",
                "next": "wait for Research Set admission",
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
    assert emitted["payload"]["checkpoint"] == "research_triage reviewed"
    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT status, json_extract(payload, '$.checkpoint') "
            "FROM operation_session WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
    assert tuple(row) == ("active", "research_triage reviewed")


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
