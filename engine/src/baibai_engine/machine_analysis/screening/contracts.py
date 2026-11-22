"""保存した候補政策の結果と、根拠にした生成世代。投資採否ではない。"""

from dataclasses import dataclass
from typing import Annotated

from pydantic import Field, JsonValue

from baibai_engine.machine_analysis.screening.discovery_contract import (
    DiscoverySelection as DiscoverySelection,
)
from baibai_engine.machine_analysis.screening.discovery_contract import discovery_publication_method
from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathSelection
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialSelection,
)
from baibai_engine.machine_analysis.screening.policies.two_views import ViewSelection

# policy.methodの区別で読む。旧payloadへ新しい既定fieldを補わない。
type ScreeningResult = (
    ViewSelection | EconomicPathSelection | TechnicalFinancialSelection | DiscoverySelection
)


@dataclass(frozen=True, slots=True)
class ScreeningSelection:
    generation_id: str
    universe_tickers: tuple[str, ...]
    result: ScreeningResult
    # 未記録の旧成果へ集計を補わず、canonical/hashを保つ。
    price_inputs: Annotated[
        dict[str, JsonValue] | None, Field(exclude_if=lambda value: value is None)
    ] = None


def validate_selection_source(
    selection: ScreeningSelection,
    *,
    selection_id: str,
    generation_id: str,
) -> None:
    if selection.generation_id != generation_id:
        raise ValueError("screening selection differs from the chosen generation")
    method = discovery_publication_method(selection_id, generation_id)
    if isinstance(selection.result, DiscoverySelection):
        if method != selection.result.policy.method:
            raise ValueError("discovery publication identity differs from its policy")
        if selection.result.tickers != selection.universe_tickers or len(
            set(selection.universe_tickers)
        ) != len(selection.universe_tickers):
            raise ValueError("discovery result differs from the exact stock universe")
    elif method is not None:
        raise ValueError("discovery publication identity differs from its format")
