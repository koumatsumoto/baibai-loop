"""保存した候補政策の結果と、根拠にした生成世代。投資採否ではない。"""

from dataclasses import dataclass

from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathSelection
from baibai_engine.machine_analysis.screening.policies.two_views import ViewSelection

# policy.methodの区別で読む。旧payloadへ新しい既定fieldを補わない。
type ScreeningResult = ViewSelection | EconomicPathSelection


@dataclass(frozen=True, slots=True)
class ScreeningSelection:
    generation_id: str
    universe_tickers: tuple[str, ...]
    result: ScreeningResult
