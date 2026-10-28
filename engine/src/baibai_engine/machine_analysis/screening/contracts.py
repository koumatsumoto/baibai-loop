"""保存した候補政策の結果と、根拠にした生成世代。投資採否ではない。"""

from dataclasses import dataclass

from baibai_engine.machine_analysis.screening.policies.two_views import ViewSelection


@dataclass(frozen=True, slots=True)
class ScreeningSelection:
    generation_id: str
    universe_tickers: tuple[str, ...]
    result: ViewSelection
