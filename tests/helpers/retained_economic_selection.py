"""旧v4の合成保存fixture。候補を選ばず、順位やpercentileを再計算しない。"""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from pydantic import TypeAdapter

from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathSelection

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "economic_path_v4.json"


def retained_economic_result(*, context=None):
    # 08b04b0b以前の合成fixtureを固定。旧31dfe3c2のcanonical hashもtestで保つ。
    result = TypeAdapter(EconomicPathSelection).validate_json(_FIXTURE.read_bytes())
    if context is None:
        return result
    return replace(result, cards=(replace(result.cards[0], context=deepcopy(context)),))
