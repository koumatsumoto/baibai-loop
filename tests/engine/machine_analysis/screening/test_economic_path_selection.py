from contextlib import contextmanager

import pytest
from tests.model_generation_fixtures import generation

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
