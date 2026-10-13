"""ユーザー指定の開始時に保存runの根拠を固定する。候補やTriageは生成しない。"""

from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from baibai_engine.foundation.time import JST
from baibai_engine.screening.run_store import ScreeningRunReader
from baibai_engine.storage.application.json import canonical_json


class ExplicitResearchRun(BaseModel):
    """開始時にcanonical runから引用した、選択銘柄だけの機械根拠。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # 候補順位は開始資格ではない。明示要求はTriageの優先選別と別の開始根拠。
    # run整理後も当時の根拠を読めるよう、選択銘柄だけを開始時に固定する。
    # docs/reference/analysis-operations.md#explicit-research-source / refs #1481

    kind: Literal["user_selected_run"] = "user_selected_run"
    run_revision_id: str = Field(min_length=1)
    as_of: date
    run_at: AwareDatetime
    run_metadata: dict[str, Any]
    analyses: dict[str, dict[str, Any]]

    @model_validator(mode="after")
    def ticker_identity(self) -> ExplicitResearchRun:
        if any(row.get("ticker") != ticker for ticker, row in self.analyses.items()):
            raise ValueError("explicit run analysis ticker differs from its key")
        return self

    def binding(self) -> dict[str, str]:
        return {
            "run_revision_id": self.run_revision_id,
            "snapshot_sha256": sha256(
                canonical_json(self.model_dump(mode="json")).encode()
            ).hexdigest(),
        }


def load_explicit_research_run(
    path: Path,
    *,
    run_revision_id: str,
    tickers: tuple[str, ...],
    known_at: datetime,
    as_of: date | None = None,
) -> ExplicitResearchRun:
    """固定runを読取のみで解決し、開始時点で利用できた銘柄分析を引用する。"""
    run = ScreeningRunReader(path).get_run(run_revision_id)
    if run is None:
        raise ValueError(f"unknown run_revision_id: {run_revision_id}")
    run_date = date.fromisoformat(run.as_of_date)
    if as_of is not None and as_of != run_date:
        raise ValueError("--asof differs from the explicitly selected analysis run")
    run_at = datetime.fromisoformat(run.run_at)
    if run_at.utcoffset() is None or known_at.utcoffset() is None:
        raise ValueError("explicit run and start time must include a timezone")
    if run_at > known_at or run_date > known_at.astimezone(JST).date():
        raise ValueError("explicit analysis run is after the Research start cutoff")
    analyses = {row["ticker"]: row for row in run.security_analyses}
    missing = set(tickers) - analyses.keys()
    if missing:
        raise ValueError(f"selected run has no Security Analysis for {', '.join(sorted(missing))}")
    return ExplicitResearchRun(
        run_revision_id=run.run_revision_id,
        as_of=run_date,
        run_at=run_at,
        run_metadata=run.payload,
        analyses={ticker: analyses[ticker] for ticker in tickers},
    )


def verify_explicit_research_run(
    source: ExplicitResearchRun, *, path: Path, as_of: date, known_at: datetime
) -> None:
    canonical = load_explicit_research_run(
        path,
        run_revision_id=source.run_revision_id,
        tickers=tuple(source.analyses),
        known_at=known_at,
        as_of=as_of,
    )
    if canonical != source:
        raise ValueError("explicit selection snapshot differs from the canonical analysis run")
