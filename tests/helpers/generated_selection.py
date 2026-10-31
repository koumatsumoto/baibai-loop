"""現行Operation用の明示生成fixture。未指定モデルや流動性を作らない。"""

import sqlite3
from contextlib import closing
from datetime import timedelta
from hashlib import sha256

from baibai_engine.investment_judgment.company_research.context import CompanyContext, NotFound
from baibai_engine.investment_judgment.company_research.query import read_company_contexts
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchInputs,
    GeneratedResearchInputsV4,
    GeneratedResearchSelection,
    GeneratedResearchSelectionDraft,
    GeneratedResearchSelectionV4,
    ResearchRequest,
)
from baibai_engine.investment_judgment.workflow.selection_policy import CURRENT_RESEARCH_CAPACITY
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot, ModelGeneration
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    EconomicPathPolicy,
    EconomicPathSelection,
)
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.primitives.json import canonical_json
from baibai_engine.primitives.time import JST
from tests.helpers.research_selection import selected_target


def seed_generated_selection(
    *, runs, now, tickers=(), scope="discover_new", models=(), screening_result=None, db=None
):
    """初回setup専用。sourceを削除した再開前には呼ばない。"""
    as_of = now.astimezone(JST).date()
    token = sha256(
        canonical_json(
            {
                "now": now.isoformat(),
                "tickers": tickers,
                "models": [model.model_dump(mode="json") for model in models],
            }
        ).encode()
    ).hexdigest()[:16]
    generation = ModelGeneration(
        generation_id=f"synthetic-{token}",
        as_of=as_of,
        evaluation_time=now - timedelta(minutes=1),
        inputs={
            "market": InputSnapshot(kind="lake_release", reference="synthetic", sha256="a" * 64)
        },
        models=tuple(models),
    )
    store = ModelGenerationStore(runs)
    store.publish(generation)
    identity = f"policy-{token}"
    ScreeningSelectionStore(runs).publish(
        identity,
        ScreeningSelection(
            generation.generation_id,
            tickers,
            screening_result
            if screening_result is not None
            else EconomicPathSelection(EconomicPathPolicy(candidate_limit=20), 0, ()),
        ),
    )
    with closing(sqlite3.connect(runs)) as connection, connection:
        connection.execute(
            "UPDATE screening_selection SET published_at=? WHERE selection_id=?",
            ((now - timedelta(seconds=30)).isoformat(), identity),
        )
    publication = read_publication(path=runs, selection_id=identity)
    value = GeneratedResearchSelection(
        request=ResearchRequest(
            text="合成例：投資提案して",
            scope=scope,
            requested_tickers=tickers if scope == "specified_stocks" else (),
        ),
        research_as_of=as_of,
        input_known_at=now,
        selection_summary="合成例：今回の対象と未調査範囲を示す",
        targets=tuple(
            selected_target(t, "holding" if scope == "review_positions" else "new") for t in tickers
        ),
        capacity=CURRENT_RESEARCH_CAPACITY,
        inputs=GeneratedResearchInputs(
            generation=store.read_subjects(generation.generation_id, subjects=("market", *tickers)),
            screening_selection_id=identity,
            screening_selection=publication.selection,
            screening_published_at=publication.published_at,
        ),
    )

    contexts = (
        read_company_contexts(db, tickers=tickers, as_of=as_of, known_at=now)
        if db is not None
        else {
            ticker: CompanyContext(research=NotFound(), assessment=NotFound()) for ticker in tickers
        }
    )
    return GeneratedResearchSelectionV4.model_validate(
        {
            **value.model_dump(mode="json"),
            "schema_version": 4,
            "inputs": GeneratedResearchInputsV4(
                **value.inputs.model_dump(), saved_company_context=contexts
            ).model_dump(mode="json"),
        }
    )


def generated_draft_for(selection):
    return GeneratedResearchSelectionDraft(
        schema_version=2,
        request=selection.request,
        research_as_of=selection.research_as_of,
        input_known_at=selection.input_known_at,
        generation_id=selection.inputs.generation.generation_id,
        screening_selection_id=selection.inputs.screening_selection_id,
        selection_summary=selection.selection_summary,
        targets=selection.targets,
    )
