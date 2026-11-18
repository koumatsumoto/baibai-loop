"""未知の銘柄を価格対権利の問いから実際の根拠へ。発見は割安・発注の認定ではない。"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from tests.engine.investment_judgment.workflow.test_technical_financial_context import seed

from baibai_engine.cli.investment_judgment import main
from baibai_engine.investment_judgment.workflow.discovery import PriceCoordinateMinimum, _explore
from baibai_engine.investment_judgment.workflow.selection import ResearchRequest
from baibai_engine.investment_judgment.workflow.selection_query import (
    load_generated_selection_context,
)
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.discovery_contract import (
    DiscoveryPolicy,
    DiscoverySelection,
    discovery_publication_id,
)
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore


def observation(ticker, **fields):
    return {
        "ticker": ticker,
        "name": None,
        "sector": None,
        "models_recorded": True,
        "screening_lane": "no_current_hypothesis",
        "financial_state": "not_selected",
        "technical_state": "not_matched",
        "matched_paths": {"growth": [], "financial": []},
        "missing": [],
        "coordinates": [],
        "price": None,
        "price_date": None,
        "expected_price_date": None,
        "basis": None,
        "price_ready": False,
        "book_equity": None,
        "capital_action": None,
        **fields,
    }


def test_observed_zero_missing_price_and_non_neutral_coverage_remain_separate():
    rights = observation(
        "1001", capital_action={"observations": 1, "share_basis_adjustments": None}
    )
    no_event = observation(
        "1002", capital_action={"observations": 0, "share_basis_adjustments": None}
    )
    negative = observation("1003", book_equity={"amount_yen": -9}, price=23, price_ready=False)
    zero = observation("1004", book_equity={"amount_yen": 0})
    absent = observation("1005", models_recorded=False)
    known = observation(
        "1006",
        screening_lane="alternative_financial_research",
        price=24,
        price_ready=True,
        matched_paths={
            "growth": ["profit_growth"],
            "financial": ["business_expansion", "operating_recovery"],
        },
        coordinates=[{"price_group": "reported", "price_value": 0.01, "business_value": -0.1}],
    )
    rows = [known, zero, negative, no_event, rights, absent]
    universe = [r["ticker"] for r in rows]
    menu = _explore(rows, universe, question=None, page=0)
    counts = {q["key"]: q["company_count"] for q in menu["questions"]}
    assert counts == {
        "earning_power": 1,
        "operating_recovery": 1,
        "reinvestment": 1,
        "reported_assets": 2,
        "capital_change": 1,
        "input_gap": 5,
        "outside_display": 5,
        "no_coordinate": 5,
        "universe": 6,
    }
    assert menu["coverage"]["without_stock_models"] == 1
    assert "rows" not in menu
    assert _explore(rows, universe, question="reported_assets", page=0)["rows"][1]["observations"][
        "book_equity"
    ] == {"amount_yen": 0}
    assert (
        _explore(rows, universe, question="capital_change", page=0)["rows"][0]["observations"]
        == rights
    )
    result = _explore(rows, universe, question="no_coordinate", page=0)["rows"]
    assert [(r["ticker"], r["observations"]["models_recorded"]) for r in result][-2:] == [
        ("1004", True),
        ("1005", False),
    ]


def test_existing_capital_and_new_investment_answer_different_questions():
    book = {
        "path_id": "capital_productivity",
        "variant": "reported_profit_latest_book",
        "price_group": "sector:book_price",
        "price_value": 1.25,
        "business_value": 0.08,
    }
    expansion = {
        "path_id": "business_expansion",
        "variant": "quarter",
        "price_group": "sector:annual_ttm_sales_price",
        "price_value": 2.0,
        "business_value": 0.03,
    }
    rows = [
        observation(
            ticker,
            matched_paths={"growth": [], "financial": [c["path_id"] for c in coordinates]},
            coordinates=coordinates,
        )
        for ticker, coordinates in [("1001", [book]), ("1002", [book, expansion])]
    ]
    universe = [r["ticker"] for r in rows]
    earnings = _explore(rows, universe, question="earning_power", page=0)
    assert [r["observations"] for r in earnings["rows"]] == rows
    investing = _explore(rows, universe, question="reinvestment", page=0)
    assert [r["ticker"] for r in investing["rows"]] == ["1002"]
    exact = PriceCoordinateMinimum("earning_power", "annual_ttm", "reported", 0.05)
    narrowed = _explore(rows, universe, question="earning_power", page=0, minimum=exact)
    assert narrowed["coordinate_comparison"]["not_comparable"] == 2
    assert narrowed["rows"] == []


def test_question_pages_reach_every_match_without_promotion_or_replacement():
    rows = [observation(str(2000 + i)) for i in range(32)]
    universe = [r["ticker"] for r in rows]
    pages = [
        _explore(tuple(reversed(rows)), universe, question="universe", page=n) for n in range(3)
    ]
    assert [p["total"] for p in pages] == [32] * 3
    assert pages[0]["next_args"] == ["--discovery-question", "universe", "--screening-page", "1"]
    assert pages[1]["next_args"] is None
    assert [r["ticker"] for p in pages for r in p["rows"]] == universe


def test_cli_question_to_one_fixed_company_and_finite_comparison(tmp_path, monkeypatch, capsys):
    import baibai_engine.machine_analysis.generation.query as generation_query
    import baibai_engine.machine_analysis.screening.selection_query as screening_query

    runs = tmp_path / "runs"
    gen, source, _ = seed(runs)
    rows = tuple(
        observation(ticker, book_equity={"amount_yen": 0} if ticker == "2345" else None)
        for ticker in source.universe_tickers
    )
    identity = discovery_publication_id(gen.generation_id)
    current = ScreeningSelection(
        gen.generation_id, source.universe_tickers, DiscoverySelection(DiscoveryPolicy(), rows, {})
    )
    ScreeningSelectionStore(runs).publish(identity, current)
    now = datetime.now(UTC) + timedelta(seconds=1)
    before = runs.read_bytes()

    def cli(args, *, expected=0):
        assert main(args, now=now) == expected
        return json.loads(capsys.readouterr().out)

    def wide(*_args, **_kwargs):
        pytest.fail("ordinary discovery must not open the old all-company publication")

    monkeypatch.setattr(screening_query, "read_publication", wide)
    real = generation_query.read_research_model_inputs
    captured = []

    def limited(generation_id, *, targets: tuple, path):
        assert generation_id == gen.generation_id
        assert path == runs
        captured.append(targets)
        return real(generation_id, targets=targets, path=path)

    monkeypatch.setattr(generation_query, "read_research_model_inputs", limited)
    base = [
        "selection-context",
        "--scope",
        "discover_new",
        "--request",
        "普通株資産を調べる",
        "--runs-path",
        str(runs),
        "--db",
        str(tmp_path / "not_an_app"),
    ]
    first = cli(base)
    output = tmp_path / "discovery-context.json"
    for _ in range(2):
        assert cli([*base, "--out", str(output)]) == first
        assert json.loads(output.read_text()) == first
    assert first["screening_selection_id"] == identity
    assert "candidate_tickers" not in first
    assert "model_inputs" not in first
    fixed = first["repeat_command_args"][1:]
    listed = cli(
        [
            *fixed,
            *next(
                q["append_args"]
                for q in first["discovery"]["questions"]
                if q["key"] == "reported_assets"
            ),
        ]
    )
    assert listed["discovery"]["total"] == 1
    assert not captured
    (row,) = listed["discovery"]["rows"]
    assert row["observations"]["book_equity"] == {"amount_yen": 0}
    evidence = cli([*fixed, *row["detail_args"], "--compare-ticker", "4567"])
    assert captured == [("2345", "4567")]
    assert [r["ticker"] for r in evidence["discovery"]["rows"]] == ["2345", "4567"]
    assert set(evidence["model_inputs"]["securities"]) == {"2345", "4567"}
    assert evidence["input_known_at"] == listed["input_known_at"] == first["input_known_at"]
    assert evidence["screening_selection_id"] == listed["screening_selection_id"] == identity
    priced = cli(
        [
            *fixed,
            "--discovery-question",
            "reported_assets",
            "--price-coordinate-min",
            "earning_power",
            "annual_ttm",
            "reported",
            "0.05",
        ]
    )
    assert priced["discovery"]["total"] == 0
    assert priced["discovery"]["coordinate_comparison"]["not_comparable"] == 1
    assert not (tmp_path / "not_an_app").exists()
    assert runs.read_bytes() == before
    absent = cli([*fixed, "--compare-ticker", "9999"])
    assert absent["discovery"]["outside_universe"] == ["9999"]
    assert not absent["model_inputs"]["securities"]["9999"]["models"]
    assert not cli([*fixed, "--screening-lane", "growth_research"])["discovery"]["rows"]
    assert cli([*fixed, "--screening-excluded"])["discovery"]["total"] == 0
    specified = cli([*fixed, "--scope", "specified_stocks", "--ticker", "4567"])
    assert list(specified["model_inputs"]["securities"]) == ["4567"]
    assert "held_tickers" not in specified
    holdings = cli([*fixed, "--scope", "review_positions"])
    assert holdings["holding_scope"] == "no_ledger_record"
    assert holdings["held_tickers"] is None
    monkeypatch.setattr(
        "baibai_engine.investment_judgment.workflow.selection_query._ledger_positions",
        lambda *_args: (("2345",), None),
    )
    held = cli([*fixed, "--scope", "review_positions"])
    assert held["held_tickers"] == ["2345"]
    assert list(held["model_inputs"]["securities"]) == ["2345"]
    with pytest.raises(ValueError, match="explicit company detail"):
        load_generated_selection_context(
            request=ResearchRequest(scope="discover_new", text="矛盾する画面"),
            generation_id=None,
            screening_selection_id=identity,
            research_as_of=None,
            known_at=now,
            runs_path=runs,
            db_path=tmp_path / "not_an_app",
            screening_lane="growth_research",
            comparison_tickers=("2345",),
        )
    # 旧世代で元本文を勝手に開かず、索引なしを候補0件としない。
    legacy = cli([*base, "--screening-selection-id", "technical-policy"], expected=3)
    assert legacy["missing_inputs"] == [{"code": "unindexed_legacy_publication"}]
    assert "candidate_tickers" not in legacy
    assert all("--compare-ticker" not in q["append_args"] for q in first["discovery"]["questions"])


def test_explicit_comparison_does_not_mix_groups_or_fill_a_missing_value():
    minimum = PriceCoordinateMinimum("earning_power", "annual_ttm", "reported", 0.05)
    coordinate = {
        "path_id": "earning_power",
        "variant": "annual_ttm",
        "price_group": "reported",
        "price_value": 0.05,
    }
    rows = [
        observation(str(3000 + i), coordinates=cs)
        for i, cs in enumerate(
            (
                [coordinate],
                [{**coordinate, "price_value": 0.04}],
                [{**coordinate, "variant": "full_fy"}],
                [{**coordinate, "price_group": "forecast"}],
                [],
                [{**coordinate, "price_value": None}],
                [coordinate, coordinate],
            )
        )
    ]
    data = _explore(
        rows, [row["ticker"] for row in rows], question="universe", page=0, minimum=minimum
    )
    assert data["coordinate_comparison"]["within_question"] == 7
    assert (
        data["coordinate_comparison"]["at_or_above"] == data["coordinate_comparison"]["below"] == 1
    )
    assert data["coordinate_comparison"]["not_comparable"] == 5
    assert [r["ticker"] for r in data["rows"]] == ["3000"]
    many = [
        observation(str(4000 + i), coordinates=[{**coordinate, "price_value": 0.06}])
        for i in range(28)
    ]
    assert _explore(
        many, [r["ticker"] for r in many], question="universe", page=0, minimum=minimum
    )["next_args"] == ["--discovery-question", "universe", *minimum.args(), "--screening-page", "1"]
    with pytest.raises(ValueError, match="finite"):
        PriceCoordinateMinimum("earning_power", "annual_ttm", "reported", float("nan"))
