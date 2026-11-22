"""指定・比較した会社だけを固定資料から読み、保有照会以外で台帳を開かない。"""

import json
import sqlite3
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from tests.engine.investment_judgment.workflow.test_discovery_questions import observation
from tests.engine.investment_judgment.workflow.test_economic_screening_context import (
    NOW,
    seed_economic_generation,
)
from tests.engine.investment_judgment.workflow.test_technical_financial_context import seed
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger

from baibai_engine.cli.investment_judgment import main
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.discovery_contract import (
    DiscoveryPolicy,
    DiscoverySelection,
    discovery_publication_id,
)
from baibai_engine.machine_analysis.screening.selection_query import (
    read_case_publication,
    read_publication,
)
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore


def command(path, app, capsys, known_at, *args, expected=0):
    code = main(
        [
            "selection-context",
            "--request",
            "同じ銘柄の記録を照合",
            "--runs-path",
            str(path),
            "--db",
            str(app),
            "--known-at",
            known_at.isoformat(),
            *args,
        ],
        now=known_at,
    )
    assert code == expected
    return json.loads(capsys.readouterr().out)


def trace_sql(monkeypatch):
    original = sqlite3.connect
    sql = []

    def traced(*args, **kwargs):
        db = original(*args, **kwargs)
        db.set_trace_callback(sql.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", traced)
    return sql


def reads(sql, table):
    return [s for s in sql if f"from {table}" in s.lower()]


@pytest.mark.parametrize("edition", ["technical", "economic"])
@pytest.mark.parametrize("scope", ["specified_stocks", "discover_new"])
def test_explicit_legacy_detail_has_only_requested_original_rows_no_ledger(
    tmp_path, monkeypatch, capsys, edition, scope
):
    runs, app = tmp_path / "runs", tmp_path / "app"
    if edition == "technical":
        _, pub, now = seed(runs)
        identity, wanted, absent = "technical-policy", "2345", "9999"
    else:
        _, pub = seed_economic_generation(runs)
        identity, wanted, absent, now = "economic-policy", "5678", "9999", NOW
    seed_ledger(
        app, load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    )
    published, source_hash, _ = read_case_publication(
        path=runs, selection_id=identity, known_at=now
    )
    before = runs.read_bytes(), app.read_bytes()
    sql = trace_sql(monkeypatch)
    result = command(
        runs,
        app,
        capsys,
        now,
        "--scope",
        scope,
        "--screening-selection-id",
        identity,
        "--ticker" if scope == "specified_stocks" else "--compare-ticker",
        wanted,
        "--compare-ticker",
        absent,
    )
    assert not reads(sql, "ledger_event")
    assert not reads(sql, "ledger_meta")
    assert "held_tickers" not in result
    assert result["candidate_tickers"] == []
    assert set(result["model_inputs"]["securities"]) == {wanted, absent}
    assert set(result["saved_company_context"]) == {wanted, absent}
    evidence = result["screening"]
    assert evidence["source_sha256"] == source_hash
    assert evidence["published_at"] == published.isoformat()
    assert evidence["not_in_saved_rows"] == [absent]
    assert evidence["outside_universe"] == ([absent] if edition == "technical" else [])
    if edition == "technical":
        expected = next(row for row in pub.result.rows if row.ticker == wanted)
        assert evidence["comparison_rows"] == [
            TypeAdapter(type(expected)).dump_python(expected, mode="json")
        ]
        assert evidence["comparison_rows"][0]["screening_lane"] == "no_current_hypothesis"
    else:
        expected = next(card for card in pub.result.cards if card.ticker == wanted)
        assert evidence["cards"] == [TypeAdapter(type(expected)).dump_python(expected, mode="json")]
        assert evidence["cards"][0]["rank"] == 1
    assert (runs.read_bytes(), app.read_bytes()) == before


@pytest.mark.parametrize("with_earlier_index", [False, True])
@pytest.mark.parametrize("scope", ["discover_new", "specified_stocks", "review_positions"])
def test_implicit_unindexed_publication_does_not_authorize_archived_evidence(
    tmp_path, monkeypatch, capsys, scope, with_earlier_index
):
    runs, app = tmp_path / "runs", tmp_path / "app"
    gen, archive, now = seed(runs)
    archived_at = read_publication(path=runs, selection_id="technical-policy").published_at
    if with_earlier_index:
        identity = discovery_publication_id(gen.generation_id)
        ScreeningSelectionStore(runs).publish(
            identity,
            ScreeningSelection(
                gen.generation_id,
                archive.universe_tickers,
                DiscoverySelection(
                    DiscoveryPolicy(), tuple(observation(t) for t in archive.universe_tickers), {}
                ),
            ),
        )
        with sqlite3.connect(runs) as connection:
            connection.execute(
                "UPDATE screening_selection SET published_at=? WHERE selection_id=?",
                ((archived_at - timedelta(microseconds=1)).isoformat(), identity),
            )
    before = runs.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("implicit unindexed lookup attempted to open evidence or a different edition")

    monkeypatch.setattr("gzip.decompress", forbidden)
    monkeypatch.setattr(
        "baibai_engine.machine_analysis.generation.query.read_research_model_inputs", forbidden
    )
    monkeypatch.setattr(
        "baibai_engine.investment_judgment.workflow.selection_query._ledger_positions", forbidden
    )
    tickers = ("--ticker", "2345") if scope == "specified_stocks" else ()
    result = command(runs, app, capsys, now, "--scope", scope, *tickers, expected=3)
    assert result["status"] == "inputs_required"
    assert result["missing_inputs"] == [{"code": "unindexed_legacy_publication"}]
    assert result["screening_selection_id"] == "technical-policy"
    assert result["generation_id"] == gen.generation_id
    assert {"candidate_tickers", "repeat_command_args", "model_inputs", "held_tickers"}.isdisjoint(
        result
    )
    assert runs.read_bytes() == before
    assert not app.exists()


@pytest.mark.parametrize("holdings", ["unimported", "empty", "held"])
def test_all_selection_editions_keep_portfolio_replay_opt_in(
    tmp_path, monkeypatch, capsys, holdings
):
    runs, app = tmp_path / "runs", tmp_path / "app"
    gen, old, _ = seed(runs)
    identity = discovery_publication_id(gen.generation_id)
    indexed = ScreeningSelection(
        gen.generation_id,
        old.universe_tickers,
        DiscoverySelection(
            DiscoveryPolicy(), tuple(observation(t) for t in old.universe_tickers), {}
        ),
    )
    ScreeningSelectionStore(runs).publish(identity, indexed)
    now = read_publication(path=runs, selection_id=identity).published_at
    if holdings != "unimported":
        ledger = load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
        seed_ledger(
            app,
            ledger
            if holdings == "held"
            else ledger.model_copy(update={"events": ledger.events[:1]}),
        )
    before = sha256(app.read_bytes()).hexdigest() if app.exists() else None
    sql = trace_sql(monkeypatch)
    for source in (
        [],
        ["--generation-id", gen.generation_id],
        ["--screening-selection-id", "technical-policy"],
        ["--screening-selection-id", identity],
    ):
        sql.clear()
        specified = command(
            runs, app, capsys, now, "--scope", "specified_stocks", "--ticker", "3456", *source
        )
        assert set(specified["model_inputs"]["securities"]) == {"3456"}
        assert "held_tickers" not in specified
        assert not reads(sql, "ledger_event")
        assert not reads(sql, "ledger_meta")
        sql.clear()
        held = command(runs, app, capsys, now, "--scope", "review_positions", *source)
        expected = ["2331"] if holdings == "held" else []
        assert set(held["model_inputs"]["securities"]) == set(expected)
        assert not held.get("candidate_tickers")
        assert held["requested_tickers"] == expected
        assert held["held_tickers"] == (None if holdings == "unimported" else expected)
        assert held["holding_scope"] == (
            "no_ledger_record"
            if holdings == "unimported"
            else "current_ledger_replayed_to_fact_cutoff"
        )
        if holdings != "unimported":
            assert len(reads(sql, "ledger_meta")) == len(reads(sql, "ledger_event")) == 1
        restricted = command(
            runs,
            app,
            capsys,
            now,
            "--scope",
            "review_positions",
            "--ticker",
            "2331",
            "--ticker",
            "3456",
            "--compare-ticker",
            "2345",
            *source,
        )
        assert restricted["request"]["requested_tickers"] == ["2331", "3456"]
        assert restricted["requested_tickers"] == ["2331", "3456"]
        assert restricted["requested_not_held"] == (
            None
            if holdings == "unimported"
            else ["3456"]
            if holdings == "held"
            else ["2331", "3456"]
        )
        assert set(restricted["model_inputs"]["securities"]) == set(expected) | {"2345"}

    assert (sha256(app.read_bytes()).hexdigest() if app.exists() else None) == before
