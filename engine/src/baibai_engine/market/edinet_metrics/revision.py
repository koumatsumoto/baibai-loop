"""EDINET metricの抽出意味revisionとbaselineのhard failure判定。"""

from collections.abc import Sequence

# 意味変更時だけ明示更新する。配置・import・transport retryは対象外。
# native CSVの科目・当期範囲・JPY単位・欠測と12か月フローを区別する。
EDINET_METRIC_REVISION = "edinet-metrics-v6-capex-total-presence"


def has_hard_metric_failure(reasons: Sequence[str]) -> bool:
    """Return whether parser provenance means no reusable metric was produced."""
    return any(
        reason == "csv_parse_failed" or reason.startswith("csv_parse_failed:") for reason in reasons
    )
