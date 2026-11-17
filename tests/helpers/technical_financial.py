"""現行の技術・財務接続testで共有する全分母の合成世代。"""

from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationSubject,
    ModelGeneration,
)
from tests.engine.machine_analysis.screening.test_financial_growth import payloads
from tests.helpers.model_generation import publish_generation
from tests.helpers.technical_discovery import _metadata, _models

_IDS = ("technical_analysis", "market_context", "company_financials", "company_growth")
_TICKERS = ("1234", "2345", "3456", "4567", "5678", "6789")


def _no_match(models):
    technical, market = models
    technical.payload["price_momentum"]["value"] = -0.2
    technical.payload["price_path"]["observations"]["R1"]["value"] = -0.1
    technical.payload["price_path"]["observations"]["R20"]["value"] = -0.2
    market.payload["price_path_relative"]["comparisons"]["R20"]["relative_return"] = -0.1
    market.payload["price"]["turnover_activity_20d"]["current_to_prior_mean_ratio"] = 0.5


def _seed(path):
    metadata = _metadata().model_copy(
        update={
            "subject_plan": {
                **{ticker: GenerationSubject(scope="stock", model_ids=_IDS) for ticker in _TICKERS},
                "market": GenerationSubject(scope="market", model_ids=("market_context",)),
            }
        }
    )
    models = []
    for ticker in _TICKERS:
        if ticker != "5678":
            technical = tuple(model.model_copy(update={"subject": ticker}) for model in _models())
            if ticker == "4567":
                _no_match(technical)
            models.extend(technical)
        if ticker == "6789":
            continue
        financial = payloads()
        financial["company_growth"]["ticker"] = ticker
        if ticker == "2345":
            balance = financial["company_growth"]["periods"][0]["observations"][-1]
            balance["metrics"]["net_assets"]["value"] = -10.0
        if ticker == "3456":
            financial.pop("company_growth")
        models.extend(
            GeneratedModel(
                model_id=name, subject=ticker, calculation_revision="synthetic-v1", payload=value
            )
            for name, value in financial.items()
        )
    fixed = ModelGeneration(**dict(metadata), models=tuple(models))
    publish_generation(fixed, path=path)
    return fixed
