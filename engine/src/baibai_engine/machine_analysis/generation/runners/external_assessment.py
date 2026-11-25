"""保存観測から現行の外部評価を作る。固定済みの過去モデルは再計算しない。"""

from datetime import datetime
from pathlib import Path

from baibai_engine.machine_analysis.models.external_assessment.analysis import analyze
from baibai_engine.machine_analysis.models.external_assessment.models import ExternalAssessment
from baibai_engine.machine_analysis.observations.external_assessments.history import (
    ObservationHistory,
)
from baibai_engine.machine_analysis.observations.external_assessments.models import (
    MonexEPSReceipt,
)
from baibai_engine.machine_analysis.observations.external_assessments.reader import (
    read_observations,
)


def external_assessment(
    path: Path, *, ticker: str, cutoff: datetime, evaluated_at: datetime
) -> ExternalAssessment:
    return analyze(
        read_observations(path, ticker, cutoff, include_retired=False), evaluated_at=evaluated_at
    )


def generate_external_assessment(
    history: ObservationHistory,
    *,
    evaluated_at: datetime,
    monex_fiscal_eps: MonexEPSReceipt | None = None,
) -> ExternalAssessment:
    return analyze(history, evaluated_at=evaluated_at, monex_fiscal_eps=monex_fiscal_eps)
