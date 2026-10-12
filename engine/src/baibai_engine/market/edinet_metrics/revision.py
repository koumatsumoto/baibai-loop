"""EDINET metricの抽出意味revisionとbaselineのhard failure判定。"""

from collections.abc import Sequence

# 旧source hashの実装と抽出意味が同一な初版。保存済み行のtokenを継承する。
# 意味変更時だけ明示更新する。配置・import・transport retryは対象外。
EDINET_METRIC_REVISION = "9b82e8d0d13ac87aaf325561de0d93fb54466178254f49856beff1b433e07187"


def has_hard_metric_failure(reasons: Sequence[str]) -> bool:
    """Return whether parser provenance means no reusable metric was produced."""
    return any(
        reason == "csv_parse_failed" or reason.startswith("csv_parse_failed:") for reason in reasons
    )
