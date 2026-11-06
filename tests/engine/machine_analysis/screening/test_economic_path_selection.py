from contextlib import contextmanager

import pytest
from tests.helpers.model_generation import generation

from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies import economic_path_selection
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    CompanyCoordinates,
    EconomicPathPolicy,
    PathCoordinates,
    select_economic_paths,
)


@pytest.mark.parametrize("subjects", [("5678", "1234", "0000"), ()])
def test_saved_generation_projects_candidates_and_missing_companies(
    tmp_path, monkeypatch, subjects
):
    path = tmp_path / "runs.sqlite"
    models = (
        GeneratedModel(
            model_id="coordinate_fixture",
            subject="1234",
            calculation_revision="v1",
            payload={"price": 1.0, "business": 1.0},
        ),
        GeneratedModel(
            model_id="context_fixture",
            subject="1234",
            calculation_revision="v2",
            payload={"support": "not_an_extra_vote"},
        ),
        GeneratedModel(
            model_id="coordinate_fixture",
            subject="5678",
            calculation_revision="v1",
            payload={"price": 2.0, "business": 2.0},
        ),
        GeneratedModel(
            model_id="coordinate_fixture",
            subject="9999",
            calculation_revision="v1",
            payload={"price": 100.0, "business": 100.0},
        ),
    )
    fixed = generation().model_copy(update={"models": models})
    projected = []

    def project_fixture(ticker, *, models, metadata):
        projected.append((ticker, tuple(model.model_id for model in models)))
        index = {model.model_id: model for model in models}
        coordinate = index.get("coordinate_fixture")
        paths = (
            ()
            if coordinate is None
            else (
                PathCoordinates(
                    path_id="business_expansion",
                    variant="quarter",
                    price_value=coordinate.payload["price"],
                    business_value=coordinate.payload["business"],
                    price_group="fixture",
                    business_group="fixture",
                    eligible=True,
                    evidence={},
                ),
            )
        )
        return CompanyCoordinates(
            ticker=ticker,
            name=None,
            sector="fixture",
            paths=paths,
            context={"generation_id": metadata.generation_id},
            model_revisions={model.model_id: model.calculation_revision for model in models},
            not_generated=(),
        )

    policy = EconomicPathPolicy()
    universe = tuple(sorted(subjects))
    expected = ScreeningSelection(
        generation_id=fixed.generation_id,
        universe_tickers=universe,
        result=select_economic_paths(
            tuple(
                project_fixture(
                    ticker,
                    models=tuple(
                        sorted(
                            (model for model in models if model.subject == ticker),
                            key=lambda model: model.model_id,
                        )
                    ),
                    metadata=fixed,
                )
                for ticker in universe
            ),
            policy=policy,
        ),
    )
    projected.clear()

    @contextmanager
    def saved_companies(generation_id, *, subjects, path):
        assert generation_id == fixed.generation_id
        assert subjects == universe
        assert path == tmp_path / "runs.sqlite"
        yield (
            fixed,
            (
                (ticker, tuple(sorted(company_models, key=lambda model: model.model_id)))
                for ticker in subjects
                if (company_models := [model for model in models if model.subject == ticker])
            ),
        )

    monkeypatch.setattr(economic_path_selection, "stream_generation_subjects", saved_companies)
    monkeypatch.setattr(economic_path_selection, "project_company", project_fixture)
    actual = economic_path_selection.select_saved_economic_generation(
        fixed.generation_id,
        tickers=subjects,
        policy=policy,
        path=path,
    )
    assert actual == expected
    assert {ticker for ticker, _ in projected} == set(subjects)
    if subjects:
        assert projected == [
            ("1234", ("context_fixture", "coordinate_fixture")),
            ("5678", ("coordinate_fixture",)),
            ("0000", ()),
        ]
        assert tuple(card.ticker for card in actual.result.cards) == ("5678", "1234")


def test_price_diagnostics_distinguish_unavailable_inputs_without_changing_selection(monkeypatch):
    fixed = generation()
    contexts = {
        "1234": {
            "price_date": "2026-10-30",
            "expected_price_date": "2026-11-02",
            "pricing_valid": False,
        },
        "5678": {
            "price_date": "2026-11-02",
            "expected_price_date": "2026-11-02",
            "pricing_valid": True,
        },
        "9999": {"price_date": None, "expected_price_date": "2026-11-02", "pricing_valid": False},
    }
    visits = []

    @contextmanager
    def stream(*args, **kwargs):
        visits.append("read")
        yield fixed, iter(())

    def project(ticker, **kwargs):
        visits.append(ticker)
        return CompanyCoordinates(ticker, None, None, (), contexts[ticker], {}, ())

    monkeypatch.setattr(economic_path_selection, "stream_generation_subjects", stream)
    monkeypatch.setattr(economic_path_selection, "project_company", project)
    result = economic_path_selection.evaluate_saved_economic_generation(
        fixed.generation_id, tickers=tuple(contexts), policy=EconomicPathPolicy()
    )
    assert result.selection.result.cards == ()
    assert result.price_inputs == {
        "universe_count": 3,
        "valid_count": 1,
        "date_mismatch_count": 1,
        "invalid_count": 2,
        "price_dates": {"2026-10-30": 1, "2026-11-02": 1, "missing": 1},
        "expected_price_dates": {"2026-11-02": 3},
    }
    assert visits == ["read", "1234", "5678", "9999"]


def test_pricing_gaps_preserve_overlapping_failures_without_reselection(monkeypatch):
    from copy import deepcopy

    from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import (
        pricing_input_failures,
    )

    fixed = generation()
    valid = {
        "expected_price_date": "2026-11-02",
        "earnings_price": {
            "price_date": "2026-11-02",
            "price_yen": 100.0,
            "shares_ex_treasury": 1000.0,
            "market_cap_yen": 100000.0,
            "unavailable_reason": None,
        },
    }
    payloads = {str(ticker): deepcopy(valid) for ticker in range(1001, 1009)}
    payloads["1002"]["earnings_price"]["unavailable_reason"] = "unavailable_annual_profit"
    for ticker in ("1003", "1004", "1005"):
        payloads[ticker]["earnings_price"].update(
            shares_ex_treasury=None, market_cap_yen=None, unavailable_reason="missing_capital_basis"
        )
    payloads["1003"]["earnings_price"]["price_date"] = "2026-10-30"
    payloads["1004"].update(
        financial={"market_cap": 500000000.0},
        provider_capital_crosscheck={"provider_market_cap_yen": 500000000.0},
    )
    payloads["1005"]["financial"] = {
        "capital_basis_failure_reason": "indeterminate_treasury_after_issued_observation"
    }
    payloads["1006"]["earnings_price"]["shares_ex_treasury"] = 0.0
    payloads["1008"]["earnings_price"]["price_date"] = "2026-10-30"
    del payloads["1007"]
    before = deepcopy(payloads)
    subjects = tuple(str(ticker) for ticker in range(1001, 1009))
    visits = []

    @contextmanager
    def stream(generation_id, *, subjects, model_ids, path):
        visits.append((generation_id, subjects, model_ids))
        yield (
            fixed,
            (
                (
                    subject,
                    (
                        GeneratedModel(
                            model_id="company_financials",
                            subject=subject,
                            calculation_revision="fixed-v1",
                            payload=payloads[subject],
                        ),
                    ),
                )
                for subject in subjects
                if subject in payloads
            ),
        )

    def no_selection(*args, **kwargs):
        pytest.fail("read-only gap inspection must not project all models or reselect")

    monkeypatch.setattr(economic_path_selection, "stream_generation_subjects", stream)
    monkeypatch.setattr(economic_path_selection, "project_company", no_selection)
    monkeypatch.setattr(economic_path_selection, "select_economic_paths", no_selection)
    selection = ScreeningSelection(
        fixed.generation_id, subjects, select_economic_paths((), policy=EconomicPathPolicy())
    )
    result = economic_path_selection.read_pricing_gaps(selection)
    assert visits == [(fixed.generation_id, subjects, ("company_financials",))]
    assert result["basis"] == "current_economic_path_pricing_gate"
    assert (result["universe_count"], result["valid_count"], result["invalid_count"]) == (8, 2, 6)
    rows = {row["ticker"]: row for row in result["companies"]}
    assert tuple(rows) == tuple(str(ticker) for ticker in range(1003, 1009))
    assert rows["1003"]["failures"] == [
        "invalid_market_cap_yen",
        "invalid_shares_ex_treasury",
        "price_date_mismatch",
    ]
    assert rows["1003"]["earnings_price"] == payloads["1003"]["earnings_price"]
    assert rows["1004"]["earnings_price"]["market_cap_yen"] is None
    assert rows["1005"]["capital_basis_failure_reason"] == (
        "indeterminate_treasury_after_issued_observation"
    )
    assert rows["1006"]["earnings_price"]["shares_ex_treasury"] == 0.0
    assert rows["1007"] == {
        "ticker": "1007",
        "model_revision": None,
        "failures": ["missing_company_financials"],
    }
    assert rows["1008"]["failures"] == ["price_date_mismatch"]
    assert all(rows[ticker]["model_revision"] == "fixed-v1" for ticker in rows if ticker != "1007")
    assert pricing_input_failures({}) == (
        "invalid_market_cap_yen",
        "invalid_price_yen",
        "invalid_shares_ex_treasury",
        "missing_expected_price_date",
    )
    assert payloads == before
