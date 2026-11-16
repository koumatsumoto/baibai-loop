"""旧v4の合成保存fixture。候補を選ばず、順位やpercentileを再計算しない。"""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from pydantic import TypeAdapter

from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    EconomicPathSelection,
    PathNormalization,
    RankedPath,
)

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "economic_path_v4.json"


def retained_economic_result(*, context=None):
    # 08b04b0b以前の合成fixtureを固定。旧31dfe3c2のcanonical hashもtestで保つ。
    result = TypeAdapter(EconomicPathSelection).validate_json(_FIXTURE.read_bytes())
    if context is None:
        return result
    return replace(result, cards=(replace(result.cards[0], context=deepcopy(context)),))


def ranked_path_fixture(path, *, percentile=0.5, population=1, midrank=1.0):
    """呼出側が指定した座標と保存順位を結ぶだけ。一社の既定値も合成値。"""
    return RankedPath(
        path_id=path.path_id,
        variant=path.variant,
        price_value=path.price_value,
        business_value=path.business_value,
        price_group=path.price_group,
        business_group=path.business_group,
        price_percentile=percentile,
        business_percentile=percentile,
        price_population=population,
        business_population=population,
        score=percentile,
        evidence=deepcopy(path.evidence),
        normalization=PathNormalization(percentile, population, midrank),
    )
