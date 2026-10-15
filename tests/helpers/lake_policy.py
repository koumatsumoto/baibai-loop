"""合成fixtureが作るdatasetだけへrelease policyを限定する。

本番releaseはprofileの必須datasetと、含めた任意datasetの条件を満たす。個別境界のテストは
対象datasetだけを作り、完全性や最低件数そのものの試験では制約を緩めない。
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from baibai_engine.storage.lake import models as lake_models

FIXTURE_DATASETS = ("jquants.daily_bars", "jquants.short_sale_reports")

# Captured at import, before any test patches the module attribute, so narrowing always
# starts from the shipping profile. A fixture that narrows further would otherwise select
# from another fixture's leftovers and find nothing.
_FULL_RELEASE_POLICY = lake_models.PRODUCTION_RELEASE_POLICY


def narrow_release_policy(
    monkeypatch: pytest.MonkeyPatch,
    *,
    datasets: Iterable[str] = FIXTURE_DATASETS,
    minimum_rows: int = 1,
    minimum_population_count: int = 1,
    carries_history: bool = False,
    relax_floors: bool = True,
) -> None:
    """Keep only `datasets` in the profile, optionally lowering their floors.

    A test about the floors themselves needs the shipping values, so it narrows the set
    without relaxing them; relaxing there would move the boundary the test is asserting.
    ``relax_floors=False`` keeps every floor.
    """

    wanted = set(datasets)
    policy = _FULL_RELEASE_POLICY
    relaxation: dict[str, object] = {}
    if relax_floors:
        relaxation = {
            "carries_history": carries_history,
            "minimum_rows": minimum_rows,
            "minimum_population_count": minimum_population_count,
        }
    monkeypatch.setattr(
        lake_models,
        "PRODUCTION_RELEASE_POLICY",
        policy.model_copy(
            update={
                "datasets": tuple(
                    item.model_copy(update=relaxation)
                    for item in policy.datasets
                    if item.dataset in wanted
                )
            }
        ),
    )
