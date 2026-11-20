"""保存した候補政策の結果と、根拠にした生成世代。投資採否ではない。"""

from dataclasses import dataclass
from typing import Annotated

from pydantic import Field, JsonValue

from baibai_engine.machine_analysis.generation.contracts import GenerationMetadata
from baibai_engine.machine_analysis.screening.discovery_contract import (
    DiscoverySelection as DiscoverySelection,
)
from baibai_engine.machine_analysis.screening.discovery_contract import (
    DiscoverySelectionV2,
    discovery_method_for_id,
    validate_discovery_publication,
)
from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathSelection
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialSelection,
)
from baibai_engine.machine_analysis.screening.policies.two_views import ViewSelection

# policy.methodの区別で読む。旧payloadへ新しい既定fieldを補わない。
type ScreeningResult = (
    ViewSelection
    | EconomicPathSelection
    | TechnicalFinancialSelection
    | DiscoverySelection
    | DiscoverySelectionV2
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
    metadata: GenerationMetadata | None,
) -> None:
    if selection.generation_id != generation_id:
        raise ValueError("screening selection differs from the chosen generation")
    if isinstance(selection.result, (DiscoverySelection, DiscoverySelectionV2)):
        validate_discovery_publication(
            selection_id=selection_id,
            generation_id=generation_id,
            result=selection.result,
            universe_tickers=selection.universe_tickers,
            metadata=metadata,
        )
    elif discovery_method_for_id(selection_id, generation_id) is not None:
        raise ValueError("discovery publication identity differs from its format")
