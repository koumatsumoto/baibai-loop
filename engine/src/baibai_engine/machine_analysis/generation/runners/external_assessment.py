"""保存済み外部評価の読取入口。UIの合成には依存しない。"""

from datetime import datetime
from pathlib import Path

from baibai_engine.machine_analysis.models.external_assessment.analysis import analyze
from baibai_engine.machine_analysis.models.external_assessment.models import ExternalAssessment
from baibai_engine.machine_analysis.observations.external_assessments.reader import read_observations


def external_assessment(
    path: Path, *, ticker: str, cutoff: datetime, evaluated_at: datetime
) -> ExternalAssessment:
    return analyze(read_observations(path, ticker, cutoff), evaluated_at=evaluated_at)
