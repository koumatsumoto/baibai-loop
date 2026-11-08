"""context再読のcutoffを渡し、固定入力からの再開との違いを保つ。"""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger
from tests.helpers.model_generation import empty_selection, generation, publish_generation

from baibai_engine.cli.investment_judgment import main
from baibai_engine.investment_judgment.company_research.document import ResearchDraft
from baibai_engine.investment_judgment.company_research.store import ResearchStoreService
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.portfolio.ledger.store import LedgerStoreService

NOW = datetime(2026, 10, 25, 0, 2, tzinfo=UTC)


def seed(tmp_path):
    runs, app = tmp_path / "runs", tmp_path / "app"
    fixed = generation()
    publish_generation(fixed, path=runs)
    ScreeningSelectionStore(runs).publish("fixed-selection", empty_selection(fixed.generation_id))
    with closing(sqlite3.connect(runs)) as connection, connection:
        connection.execute(
            "UPDATE screening_selection SET published_at=?",
            ((NOW - timedelta(minutes=1)).isoformat(),),
        )
    arguments = [
        "selection-context",
        "--request",
        "価格basisの不足を同じ断面で確認する",
        "--scope",
        "discover_new",
        "--screening-selection-id",
        "fixed-selection",
        "--compare-ticker",
        "1234",
        "--runs-path",
        str(runs),
        "--db",
        str(app),
    ]
    return runs, app, arguments


@pytest.mark.parametrize("cutoff", ["2026-10-25T00:02:00+00:00", "2026-10-25T09:02:00+09:00"])
def test_explicit_cutoff_excludes_later_recorded_research(tmp_path, capsys, cutoff):
    runs, app, arguments = seed(tmp_path)
    document = {
        "ticker": "1234",
        "as_of": "2026-10-23",
        "investigated_scope": "株数原典の対象日を照合中",
        "unresolved_questions": ["同一基準日の自己株数を確認する"],
    }
    first = ResearchStoreService(app, clock=lambda: NOW - timedelta(seconds=30)).save(
        ResearchDraft(research_id="first", document=document)
    )
    arguments += ["--research-asof", document["as_of"]]
    assert main(arguments, now=NOW) == 0
    original = json.loads(capsys.readouterr().out)
    assert original["candidate_tickers"] == []
    assert original["saved_company_context"]["1234"]["research"]["reference"] == (
        first.reference.model_dump(mode="json")
    )
    ResearchStoreService(app, clock=lambda: NOW + timedelta(hours=1)).save(
        ResearchDraft(research_id="later", prior_research_ref=first.reference, document=document)
    )
    before = runs.read_bytes(), app.read_bytes()
    later_now = NOW + timedelta(days=1)
    assert main([*arguments, "--known-at", cutoff], now=later_now) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert datetime.fromisoformat(repeated["input_known_at"]) == NOW
    repeated["input_known_at"] = original["input_known_at"]
    assert repeated == original

    # 省略時は現在の照合を続ける。モデルは同じでも、後のResearchは読める。
    assert main(arguments, now=later_now) == 0
    current = json.loads(capsys.readouterr().out)
    assert current["input_known_at"] == later_now.isoformat()
    assert current["saved_company_context"]["1234"]["research"]["reference"]["research_id"] == (
        "later"
    )
    assert current["model_inputs"] == original["model_inputs"]
    assert (runs.read_bytes(), app.read_bytes()) == before


def test_same_cutoff_does_not_freeze_late_reported_ledger_events(tmp_path, capsys):
    runs, app, arguments = seed(tmp_path)
    source = load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    initial = source.model_copy(update={"events": source.events[:1]})
    seed_ledger(app, initial)
    arguments[arguments.index("discover_new")] = "review_positions"
    arguments += ["--known-at", NOW.isoformat()]
    assert main(arguments, now=NOW) == 0
    original = json.loads(capsys.readouterr().out)
    assert original["held_tickers"] == original["requested_tickers"] == []

    # 発生時刻がcutoff以前でも、最初の読取後に届く取引事実はある。
    later_now = NOW + timedelta(days=1)
    writer = LedgerStoreService(app)
    writer.apply_document(
        expected_head=writer.load_with_head()[1],
        expected_document=initial,
        replacement=source.model_copy(update={"as_of": later_now}),
    )
    before = runs.read_bytes(), app.read_bytes()
    assert main(arguments, now=later_now) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["input_known_at"] == original["input_known_at"]
    assert repeated["research_as_of"] == original["research_as_of"]
    assert repeated["generation_id"] == original["generation_id"]
    assert repeated["screening_selection_id"] == original["screening_selection_id"]
    assert repeated["held_tickers"] == repeated["requested_tickers"] == ["2331"]
    assert (runs.read_bytes(), app.read_bytes()) == before


def test_default_research_date_follows_explicit_cutoff_not_execution_date(tmp_path, capsys):
    runs, app, arguments = seed(tmp_path)
    before = runs.read_bytes()
    assert main([*arguments, "--known-at", NOW.isoformat()], now=NOW + timedelta(days=1)) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["research_as_of"] == "2026-10-25"
    assert output["input_known_at"] == NOW.isoformat()
    assert runs.read_bytes() == before
    assert not app.exists()


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        (["--known-at", "2026-10-25T00:02:00"], "input cutoff must have a timezone"),
        (
            ["--known-at", "2026-10-25T00:00:00+00:00"],
            "unknown or not yet published screening selection",
        ),
        (
            ["--known-at", NOW.isoformat(), "--research-asof", "2026-10-26"],
            "Research date/cutoff is invalid",
        ),
    ],
)
def test_cutoff_reuses_existing_timezone_date_and_publication_checks(
    tmp_path, capsys, extra, reason
):
    runs, app, arguments = seed(tmp_path)
    before = runs.read_bytes()
    assert main([*arguments, *extra], now=NOW + timedelta(days=1)) == 3
    assert json.loads(capsys.readouterr().out)["reason"] == reason
    assert runs.read_bytes() == before
    assert not app.exists()
