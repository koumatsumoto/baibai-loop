"""Narrow the release profile to the datasets a fixture actually builds.

The shipping profile requires every dataset the registry declares, because a release
that silently omits one would send a reader to the legacy store for the rest. A fixture
that builds two tables is not that situation: it is a test of the code around the
release, not of the store's completeness. Narrowing the profile keeps those tests
failing for their own reasons rather than for the fixture's shape, and keeps the real
completeness rule un-relaxed where it matters.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import pytest

from baibai_engine.market.lake import models as lake_models

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
    age_days: int = 10_000,
    relax_floors: bool = True,
) -> None:
    """Keep only `datasets` in the profile, optionally lowering their floors.

    A test about the floors themselves needs the shipping values, so it narrows the set
    without relaxing them; relaxing there would move the boundary the test is asserting.
    """

    wanted = set(datasets)
    policy = _FULL_RELEASE_POLICY
    relaxation: dict[str, object] = (
        {
            "coverage_start_on_or_before": date.max,
            "minimum_rows": minimum_rows,
            "minimum_population_count": minimum_population_count,
            "max_age_days": age_days,
            "max_lead_days": age_days,
        }
        if relax_floors
        else {}
    )
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
