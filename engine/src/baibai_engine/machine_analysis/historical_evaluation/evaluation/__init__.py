"""較正の評価指標: rank IC・decile・候補replay・trap・条件付きspread・収束実現。

統計の誠実性 (docs/doctrine.md の計測経路):
- cohort (月次 asof) は forward 窓が重複し独立でないため、有意性検定は行わず
  「効果量 (median/mean excess) と cohort 勝率」で報告する。
- 超過リターンの一次基準はcommon eligible母集団の中央値。構成差や交絡を
  除去した選定効果の因果推定ではない。
  benchmark ETF は市況文脈の参考値。
- 個別銘柄の年率換算は E[r] との較正比較に使う。売買・資金配分を再現した
  ポートフォリオの累積収益・年率収益・Sharpe 等の運用実績は主張しない。"""

from baibai_engine.machine_analysis.historical_evaluation.evaluation.axes import (
    MIN_HALF_SPLIT_SAMPLE as MIN_HALF_SPLIT_SAMPLE,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.cohorts import (
    evaluate_cohorts as evaluate_cohorts,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.policy import (
    ASSET_BACKED_CONTROL_FIELDS as ASSET_BACKED_CONTROL_FIELDS,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.policy import (
    ASSET_BACKED_THRESHOLD as ASSET_BACKED_THRESHOLD,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.policy import AXES as AXES
from baibai_engine.machine_analysis.historical_evaluation.evaluation.policy import (
    COMPARISON_TOP_NS as COMPARISON_TOP_NS,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.policy import (
    DECILES as DECILES,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.policy import (
    DETERIORATION_THRESHOLD as DETERIORATION_THRESHOLD,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.policy import (
    GATE_BASE_AXES as GATE_BASE_AXES,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.policy import (
    MARGIN_CONTROL_FIELDS as MARGIN_CONTROL_FIELDS,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.policy import (
    MARGIN_HYPOTHESIS_AXES as MARGIN_HYPOTHESIS_AXES,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.policy import (
    MIN_AXIS_SAMPLE as MIN_AXIS_SAMPLE,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.policy import (
    MIN_IC_SAMPLE as MIN_IC_SAMPLE,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.policy import (
    MIN_QUALITY_CONTROL_GROUP as MIN_QUALITY_CONTROL_GROUP,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.policy import (
    MIN_THRESHOLD_REMOVED_SAMPLE as MIN_THRESHOLD_REMOVED_SAMPLE,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.policy import (
    PROFIT_NORMALIZATION_CONTROL_FIELDS as PROFIT_NORMALIZATION_CONTROL_FIELDS,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.policy import (
    RETURN_CHANGE_COMPONENT_FIELDS as RETURN_CHANGE_COMPONENT_FIELDS,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.policy import (
    RETURN_CHANGE_CONTROL_FIELDS as RETURN_CHANGE_CONTROL_FIELDS,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.policy import (
    REVERSION_AXES as REVERSION_AXES,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.policy import (
    SECTOR_MEDIAN_AXES as SECTOR_MEDIAN_AXES,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.policy import (
    TRAP_EXCESS_THRESHOLD as TRAP_EXCESS_THRESHOLD,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.policy import (
    AxisSpec as AxisSpec,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.sensitivity import (
    OPTIONAL_SENSITIVITY_METRICS as OPTIONAL_SENSITIVITY_METRICS,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.sensitivity import (
    delisting_exclusion_sensitivity as delisting_exclusion_sensitivity,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.sensitivity import (
    priced_master_without_universe_sensitivity as priced_master_without_universe_sensitivity,
)
