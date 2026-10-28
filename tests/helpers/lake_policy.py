"""合成fixtureが作るdatasetだけへrelease policyを限定する。

本番releaseはprofileの必須datasetと、含めた任意datasetの条件を満たす。個別境界のテストは
対象datasetだけを作り、coverage・原本の意味・世代一致はそのまま検証する。
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from baibai_engine.storage.snapshots import models as lake_models
from baibai_jobs.publication import policy as snapshot_policy

FIXTURE_DATASETS = ("jquants.daily_bars", "jquants.short_sale_reports")

# Captured at import, before any test patches the module attribute, so narrowing always
# starts from the shipping profile. A fixture that narrows further would otherwise select
# from another fixture's leftovers and find nothing.
_FULL_RELEASE_POLICY = snapshot_policy.PRODUCTION_RELEASE_POLICY


def narrow_release_policy(
    monkeypatch: pytest.MonkeyPatch,
    *,
    datasets: Iterable[str] = FIXTURE_DATASETS,
    carries_history: bool | None = False,
) -> None:
    """Limit the fixture's datasets; None keeps their normal history behavior."""
    wanted = set(datasets)
    policy = _FULL_RELEASE_POLICY
    overrides = {} if carries_history is None else {"carries_history": carries_history}
    monkeypatch.setattr(
        snapshot_policy,
        "PRODUCTION_RELEASE_POLICY",
        policy.model_copy(
            update={
                "datasets": tuple(
                    item.model_copy(update=overrides)
                    for item in policy.datasets
                    if item.dataset in wanted
                )
            }
        ),
    )
