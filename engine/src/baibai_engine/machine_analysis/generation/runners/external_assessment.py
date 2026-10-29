"""保存済み外部評価の読取入口。UIの合成には依存しない。"""

from datetime import datetime
from pathlib import Path

from baibai_engine.machine_analysis.models.external_assessment.analysis import analyze
from baibai_engine.machine_analysis.models.external_assessment.models import ExternalAssessment
from baibai_engine.machine_analysis.observations.external_assessments.history import (
    ObservationHistory,
    TradingViewHistory,
)
from baibai_engine.machine_analysis.observations.external_assessments.reader import (
    read_observations,
)


def external_assessment(
    path: Path, *, ticker: str, cutoff: datetime, evaluated_at: datetime
) -> ExternalAssessment:
    return analyze(read_observations(path, ticker, cutoff), evaluated_at=evaluated_at)


def generate_external_assessment(
    history: ObservationHistory, *, evaluated_at: datetime
) -> ExternalAssessment:
    # Personal historical reads are not a licence for automated decision inputs.
    # Also exclude explicitly supplied histories, not just the SQLite batch reader.
    result = analyze(
        history.model_copy(update={"tradingview": TradingViewHistory()}), evaluated_at=evaluated_at
    )
    return result.model_copy(
        update={
            "method_version": "same-series-v5-no-tradingview",
            "tradingview": result.tradingview.model_copy(
                update={"availability": "excluded_by_source_terms", "comparisons": ()}
            ),
        }
    )
