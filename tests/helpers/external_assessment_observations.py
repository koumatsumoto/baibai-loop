"""出所別の合成外部評価。保存/解析のtestをimportしない共有入力。"""

from datetime import UTC, datetime

from baibai_engine.machine_analysis.observations.external_assessments.models import (
    AnalystRatingObservation,
    ModelValuationObservation,
)

RECEIVED = datetime(2026, 10, 10, 16, 30, 0, 123456, tzinfo=UTC)


def valuation(at=RECEIVED, **changes):
    return ModelValuationObservation.model_validate(
        {
            "ticker": "1234",
            "fetched_at_utc": at,
            "distributor": "monex",
            "source_url": "https://example.invalid/model/1234",
            "parser_version": "synthetic-1",
            "consensus_target": 1200,
            "currency": "JPY",
            "unit": "currency_per_share",
            "share_basis": "synthetic-common-stock-2026",
            "consensus_target_origin": "ifis",
            "consensus_target_period": "12-month-target",
            "consensus_target_count": 4,
            **changes,
        }
    )


def rating(at=RECEIVED, **changes):
    return AnalystRatingObservation.model_validate(
        {
            "ticker": "1234",
            "fetched_at_utc": at,
            "distributor": "rakuten",
            "origin": "ifis",
            "source_url": "https://example.invalid/rating/1234",
            "parser_version": "synthetic-1",
            "rating": 3.5,
            "rating_scale_min": 1,
            "rating_scale_max": 5,
            "rating_scale_direction": "higher_is_positive",
            "rating_count": 4,
            **changes,
        }
    )
