"""生成済みcontextのcutoff・欠測・比較対象を、旧runへ戻らず保持する。"""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from tests.helpers.db_seed import seed_ledger
from tests.helpers.generated_selection import seed_generated_selection
from tests.helpers.ledger import load_portfolio_ledger

from baibai_engine.investment_judgment.workflow import selection_query
from baibai_engine.investment_judgment.workflow.selection import ResearchRequest
from baibai_engine.investment_judgment.workflow.selection_query import (
    load_generated_selection_context,
)
from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    CandidateCard,
    EconomicPathPolicy,
    EconomicPathSelection,
    RankedPath,
)
from baibai_engine.portfolio.ledger.events import ContributionEvent
from baibai_engine.portfolio.ledger.store import LedgerStoreService

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")


def context(path, *, selected=None, known_at=NOW, comparisons=(), scope="discover_new", tickers=()):
    return load_generated_selection_context(
        request=ResearchRequest(text="request", scope=scope, requested_tickers=tickers),
        research_as_of=None,
        generation_id=None if selected is None else selected.inputs.generation.generation_id,
        screening_selection_id=None if selected is None else selected.inputs.screening_selection_id,
        known_at=known_at,
        runs_path=path,
        db_path=path.parent / "absent-app",
        comparison_tickers=comparisons,
    )


def test_missing_generation_and_empty_published_candidates_are_distinct(tmp_path):
    path = tmp_path / "runs"
    assert context(path)["missing_inputs"] == [{"code": "no_model_publication"}]
    assert not path.exists()
    selected = seed_generated_selection(runs=path, now=NOW, tickers=("1234",))
    result = context(path, selected=selected, comparisons=("1234",))
    assert result["status"] == "ready"
    assert result["candidate_tickers"] == []
    assert result["model_inputs"]["securities"]["1234"]["not_generated"]
    assert not (tmp_path / "absent-app").exists()


def test_comparisons_do_not_change_request_policy_or_saved_observations(tmp_path):
    path = tmp_path / "runs"
    models = (
        GeneratedModel(
            model_id="company_valuation",
            subject="1234",
            calculation_revision="v1",
            payload={"earnings_price": {"earnings_yield": 0.1}},
        ),
        GeneratedModel(
            model_id="company_valuation",
            subject="5678",
            calculation_revision="v1",
            payload={"earnings_price": {"earnings_yield": None}, "observed_zero": 0},
        ),
    )
    selected = seed_generated_selection(
        runs=path,
        now=NOW,
        tickers=("1234", "5678"),
        models=models,
        screening_result=EconomicPathSelection(
            EconomicPathPolicy(candidate_limit=20),
            1,
            (
                CandidateCard(
                    ticker="1234",
                    name=None,
                    sector=None,
                    rank=1,
                    priority=0.5,
                    strongest_path="earning_power",
                    paths=(
                        RankedPath(
                            "earning_power",
                            "annual_ttm",
                            0.1,
                            0.25,
                            "market",
                            "sector",
                            0.5,
                            0.5,
                            1,
                            1,
                            0.5,
                            {},
                        ),
                    ),
                    context={},
                    model_revisions={},
                    not_generated=(),
                ),
            ),
        ),
    )
    before = path.read_bytes()
    baseline = context(path, selected=selected)
    result = context(path, selected=selected, comparisons=("5678", "9999", "5678"))
    assert result["request"] == baseline["request"]
    assert result["request"]["requested_tickers"] == []
    assert result["screening"] == baseline["screening"]
    assert result["candidate_tickers"] == ["1234"]
    assert result["capacity"] == baseline["capacity"]
    observed = result["model_inputs"]["securities"]["5678"]["company_valuation"]
    assert observed == models[1].payload
    assert result["model_inputs"]["securities"]["9999"]["not_generated"]
    assert path.read_bytes() == before


def test_timezone_equivalent_cutoffs_use_same_jst_research_date(tmp_path):
    path = tmp_path / "absent"
    for known_at in (
        datetime.fromisoformat("2026-10-14T00:30:00+09:00"),
        datetime.fromisoformat("2026-10-13T15:30:00+00:00"),
    ):
        assert context(path, known_at=known_at)["research_as_of"] == "2026-10-14"


@pytest.mark.parametrize("source", ["generation", "screening"])
def test_unknown_explicit_source_does_not_fall_back_to_latest(tmp_path, source):
    path = tmp_path / "runs"
    selected = seed_generated_selection(runs=path, now=NOW)
    with pytest.raises(ValueError, match=r"differs|unknown|unavailable"):
        load_generated_selection_context(
            request=ResearchRequest(
                text="request", scope="specified_stocks", requested_tickers=("1234",)
            ),
            research_as_of=None,
            generation_id="missing"
            if source == "generation"
            else selected.inputs.generation.generation_id,
            screening_selection_id="missing"
            if source == "screening"
            else selected.inputs.screening_selection_id,
            known_at=NOW,
            runs_path=path,
            db_path=tmp_path / "absent-app",
        )


def test_future_publication_is_not_replaced_by_current_or_zero_candidates(tmp_path):
    path = tmp_path / "runs"
    selected = seed_generated_selection(runs=path, now=NOW)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE screening_selection SET published_at=?",
            ((NOW + timedelta(seconds=1)).isoformat(),),
        )
    with pytest.raises(ValueError, match="not yet published"):
        context(path, selected=selected)
    assert context(path)["status"] == "inputs_required"


def test_missing_model_never_hides_broken_source_schema(tmp_path):
    path = tmp_path / "runs"
    seed_generated_selection(runs=path, now=NOW)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(RuntimeError, match="schema"):
        context(path, comparisons=("9999",))


def test_selection_context_keeps_ledger_metadata_and_events_in_one_snapshot(tmp_path, monkeypatch):
    app = tmp_path / "absent-app"
    source = load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    seed_ledger(app, source)
    with closing(sqlite3.connect(app)) as setup:
        setup.execute("PRAGMA journal_mode=WAL")
    writer = LedgerStoreService(app)
    head = writer.append_head()
    later = source.as_of + timedelta(minutes=1)
    event = ContributionEvent(
        type="contribution",
        event_id="concurrent-deposit",
        occurred_at=later.isoformat(),
        amount_yen=100,
    )
    replacement = source.model_copy(update={"as_of": later, "events": (*source.events, event)})
    original_connect = selection_query.connect_read_only
    committed = []

    def connect(path):
        connection = original_connect(path)

        def between_meta_and_events(sql):
            if "FROM ledger_event" in sql and not committed:
                committed.append(True)
                writer.apply_document(
                    expected_head=head,
                    expected_document=source,
                    replacement=replacement,
                )

        connection.set_trace_callback(between_meta_and_events)
        return connection

    monkeypatch.setattr(selection_query, "connect_read_only", connect)
    runs = tmp_path / "runs"
    selected = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",))
    result = context(runs, selected=selected)
    assert result["status"] == "ready"
    assert committed == [True]
    assert writer.load() == replacement
