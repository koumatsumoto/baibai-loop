"""新しい有限Caseの証拠。Runsの固定後、App writerの断面から選択を完成する。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from baibai_engine.investment_judgment.company_research.context import Unreadable
from baibai_engine.investment_judgment.company_research.query import company_context
from baibai_engine.investment_judgment.market_assessment.projection import assessment_projection
from baibai_engine.investment_judgment.market_assessment.query import (
    latest_assessment_from_connection,
)
from baibai_engine.investment_judgment.workflow.case_screening import (
    read_case_screening_from_connection,
)
from baibai_engine.investment_judgment.workflow.selection import (
    CaseScreening,
    GeneratedResearchInputsV5,
    GeneratedResearchSelectionDraft,
    GeneratedResearchSelectionV5,
    selection_sha256,
)
from baibai_engine.investment_judgment.workflow.selection_policy import research_capacity
from baibai_engine.machine_analysis.generation.contracts import ModelInputSlice
from baibai_engine.machine_analysis.generation.query import (
    read_research_model_inputs_from_connection,
)
from baibai_engine.machine_analysis.models.company_growth.models import CompanyGrowth
from baibai_engine.storage.sqlite.machine_analysis import connect_read_only

type FrozenRuns = tuple[ModelInputSlice, tuple[datetime, CaseScreening] | None]


def freeze_runs(draft: GeneratedResearchSelectionDraft, runs_path: Path) -> FrozenRuns:
    if draft.request.scope == "discover_new" and draft.screening_selection_id is None:
        raise ValueError("discovery requires an explicit saved screening selection")
    subjects = (*[t.ticker for t in draft.targets], *draft.comparison_tickers)
    with closing(connect_read_only(runs_path)) as reader:
        reader.execute("BEGIN")
        publication = None
        if draft.screening_selection_id is not None:
            publication = read_case_screening_from_connection(
                reader,
                draft.screening_selection_id,
                subjects=subjects,
                known_at=draft.input_known_at,
            )
            if publication is None or publication[1].generation_id != draft.generation_id:
                raise ValueError("screening selection differs from the chosen generation")
            if publication[0].utcoffset() is None or publication[0] > draft.input_known_at:
                raise ValueError("screening publication clock is unknown or after input_known_at")
            if (
                draft.screening_source_sha256 is not None
                and publication[1].source_sha256 != draft.screening_source_sha256
            ):
                raise ValueError("screening publication differs from the supplied source hash")
        models = read_research_model_inputs_from_connection(
            reader, draft.generation_id, targets=subjects
        )
    if models is None:
        raise ValueError("chosen model generation is unavailable")
    if models.evaluation_time > draft.input_known_at or models.as_of > draft.research_as_of:
        raise ValueError("generation evaluation_time or as_of is after the Research cutoff")
    # 調査対象と明示した比較会社だけ、投影で省略する期間も含めて正本を確かめる。
    for model in models.models:
        if (
            model.model_id == "company_growth"
            and model.subject in subjects
            and CompanyGrowth.model_validate(model.payload).ticker != model.subject
        ):
            raise ValueError("growth payload belongs to a different subject")
    return models, publication


def select_in_app(
    connection: sqlite3.Connection,
    draft: GeneratedResearchSelectionDraft,
    fixed: FrozenRuns,
) -> GeneratedResearchSelectionV5:
    models, publication = fixed
    market = latest_assessment_from_connection(
        connection, as_of=draft.research_as_of, known_at=draft.input_known_at
    )
    companies = {
        t.ticker: company_context(
            connection, t.ticker, as_of=draft.research_as_of, known_at=draft.input_known_at
        )
        for t in draft.targets
    }
    for company in companies.values():
        for evidence in (company.research, company.assessment):
            if isinstance(evidence, Unreadable):
                raise ValueError(f"cannot fix unreadable company evidence: {evidence.reason}")
    selection = GeneratedResearchSelectionV5(
        comparison_tickers=draft.comparison_tickers,
        request=draft.request,
        research_as_of=draft.research_as_of,
        input_known_at=draft.input_known_at,
        selection_summary=draft.selection_summary,
        targets=draft.targets,
        capacity=research_capacity(draft.request.scope, len(draft.targets)),
        inputs=GeneratedResearchInputsV5(
            generation=models,
            screening_selection_id=draft.screening_selection_id,
            screening_selection=None if publication is None else publication[1],
            screening_published_at=None if publication is None else publication[0],
            saved_market_context_assessment=None
            if market is None
            else assessment_projection(market, as_of=draft.research_as_of),
            saved_company_context=companies,
        ),
    )
    if (
        draft.expected_selection_sha256 is not None
        and selection_sha256(selection) != draft.expected_selection_sha256
    ):
        raise ValueError("selected Case differs from its supplied hash")
    return selection
