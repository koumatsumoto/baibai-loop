"""保存済み外部評価の読取入口。領域はUI合成read_apiへ依存しない。"""

from datetime import datetime
from pathlib import Path

from .analysis import analyze
from .models import ExternalAssessment
from .reader import read_observations


def external_assessment(
    path: Path, *, ticker: str, cutoff: datetime, evaluated_at: datetime
) -> ExternalAssessment:
    return analyze(read_observations(path, ticker, cutoff), evaluated_at=evaluated_at)
