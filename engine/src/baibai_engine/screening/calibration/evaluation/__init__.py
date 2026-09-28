"""較正の評価指標: rank IC・decile・Review Set replay・trap・条件付きspread・収束実現。

統計の誠実性 (docs/doctrine.md の計測経路):
- cohort (月次 asof) は forward 窓が重複し独立でないため、有意性検定は行わず
  「効果量 (median/mean excess) と cohort 勝率」で報告する。
- 超過リターンの一次基準はcommon eligible母集団の中央値 (選定スキルの直接計測) 。
  benchmark ETF は市況文脈の参考値。
- 累積リターン・年率・シャープ等の track record 系は出力しない。"""

from .axes import MIN_HALF_SPLIT_SAMPLE as MIN_HALF_SPLIT_SAMPLE
from .cohorts import evaluate_cohorts as evaluate_cohorts
from .policy import ASSET_BACKED_CONTROL_FIELDS as ASSET_BACKED_CONTROL_FIELDS
from .policy import ASSET_BACKED_THRESHOLD as ASSET_BACKED_THRESHOLD
from .policy import AXES as AXES
from .policy import COMPARISON_TOP_NS as COMPARISON_TOP_NS
from .policy import DECILES as DECILES
from .policy import DETERIORATION_THRESHOLD as DETERIORATION_THRESHOLD
from .policy import GATE_BASE_AXES as GATE_BASE_AXES
from .policy import MARGIN_CONTROL_FIELDS as MARGIN_CONTROL_FIELDS
from .policy import MARGIN_HYPOTHESIS_AXES as MARGIN_HYPOTHESIS_AXES
from .policy import MIN_AXIS_SAMPLE as MIN_AXIS_SAMPLE
from .policy import MIN_IC_SAMPLE as MIN_IC_SAMPLE
from .policy import MIN_QUALITY_CONTROL_GROUP as MIN_QUALITY_CONTROL_GROUP
from .policy import MIN_THRESHOLD_REMOVED_SAMPLE as MIN_THRESHOLD_REMOVED_SAMPLE
from .policy import PROFIT_NORMALIZATION_CONTROL_FIELDS as PROFIT_NORMALIZATION_CONTROL_FIELDS
from .policy import RETURN_CHANGE_COMPONENT_FIELDS as RETURN_CHANGE_COMPONENT_FIELDS
from .policy import RETURN_CHANGE_CONTROL_FIELDS as RETURN_CHANGE_CONTROL_FIELDS
from .policy import REVERSION_AXES as REVERSION_AXES
from .policy import SECTOR_MEDIAN_AXES as SECTOR_MEDIAN_AXES
from .policy import TRAP_EXCESS_THRESHOLD as TRAP_EXCESS_THRESHOLD
from .policy import AxisSpec as AxisSpec
from .sensitivity import OPTIONAL_SENSITIVITY_METRICS as OPTIONAL_SENSITIVITY_METRICS
from .sensitivity import delisting_exclusion_sensitivity as delisting_exclusion_sensitivity
from .sensitivity import (
    priced_master_without_universe_sensitivity as priced_master_without_universe_sensitivity,
)
