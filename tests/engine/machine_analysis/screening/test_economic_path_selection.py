import pytest
from tests.helpers.model_generation import generation

from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies import economic_path_selection
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    CompanyCoordinates,
    EconomicPathPolicy,
    PathCoordinates,
    select_economic_paths,
)


@pytest.mark.parametrize("subjects", [("5678", " 1234 ", "0000"), ()])
def test_coordinates_select_fixed_universe_and_project_only_missing_companies(
    monkeypatch, subjects
):
    fixed = generation()
    coordinates = {
        ticker: CompanyCoordinates(
            ticker=ticker,
            name=None,
            sector="fixture",
            paths=(
                PathCoordinates(
                    path_id="business_expansion",
                    variant="quarter",
                    price_value=value,
                    business_value=value,
                    price_group="fixture",
                    business_group="fixture",
                    eligible=True,
                    evidence={},
                ),
            ),
            context={},
            model_revisions={},
            not_generated=(),
        )
        for ticker, value in (("1234", 1.0), ("5678", 2.0), ("9999", 100.0))
    }
    missing = CompanyCoordinates("0000", None, None, (), {}, {}, ())
    projected = []

    def project_missing(ticker, *, models, metadata):
        assert models == ()
        assert metadata is fixed
        projected.append(ticker)
        return missing

    monkeypatch.setattr(economic_path_selection, "project_company", project_missing)
    universe = ("0000", "1234", "5678") if subjects else ()
    policy = EconomicPathPolicy()
    expected = ScreeningSelection(
        generation_id=fixed.generation_id,
        universe_tickers=universe,
        result=select_economic_paths(
            tuple(missing if ticker == "0000" else coordinates[ticker] for ticker in universe),
            policy=policy,
        ),
    )
    actual = economic_path_selection.evaluate_economic_coordinates(
        fixed, tickers=subjects, coordinates=coordinates, policy=policy
    )
    assert actual.selection.generation_id == expected.generation_id
    assert actual.selection.universe_tickers == expected.universe_tickers
    assert actual.selection.result == expected.result
    assert actual.selection.price_inputs == actual.price_inputs
    assert projected == (["0000"] if subjects else [])
    if subjects:
        assert tuple(card.ticker for card in actual.selection.result.cards) == ("5678", "1234")


def test_price_diagnostics_distinguish_unavailable_inputs_without_changing_selection():
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
    coordinates = {
        ticker: CompanyCoordinates(ticker, None, None, (), context, {}, ())
        for ticker, context in contexts.items()
    }
    result = economic_path_selection.evaluate_economic_coordinates(
        generation(),
        tickers=tuple(contexts),
        coordinates=coordinates,
        policy=EconomicPathPolicy(),
    )
    assert result.selection.result.cards == ()
    assert result.selection.price_inputs == result.price_inputs
    assert result.price_inputs == {
        "universe_count": 3,
        "valid_count": 1,
        "date_mismatch_count": 1,
        "invalid_count": 2,
        "price_dates": {"2026-10-30": 1, "2026-11-02": 1, "missing": 1},
        "expected_price_dates": {"2026-11-02": 3},
    }


def test_duplicate_normalized_universe_is_rejected():
    with pytest.raises(ValueError, match="unique companies"):
        economic_path_selection.evaluate_economic_coordinates(
            generation(), tickers=("1234", " 1234 "), coordinates={}, policy=EconomicPathPolicy()
        )
