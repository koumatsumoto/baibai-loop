"""一度発行された探索から、Caseが実際に使った行と当時の母集団を固定する。"""

import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from pydantic import TypeAdapter

from baibai_engine.investment_judgment.workflow.selection import CaseScreening, PopulationGroup
from baibai_engine.machine_analysis.screening.contracts import ScreeningResult
from baibai_engine.machine_analysis.screening.discovery_contract import DiscoverySelection
from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathSelection
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialSelection,
)
from baibai_engine.machine_analysis.screening.selection_query import (
    read_case_publication_from_connection,
)
from baibai_engine.primitives.json import encode_json
from baibai_engine.storage.sqlite.machine_analysis import connect_read_only


def _state(value: object) -> str | None:
    return value if isinstance(value, str) else None


def read_case_screening(
    selection_id: str, *, subjects: tuple[str, ...], known_at: datetime, path: Path
) -> tuple[datetime, CaseScreening] | None:
    if not path.is_file():
        return None
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        return read_case_screening_from_connection(
            connection, selection_id, subjects=subjects, known_at=known_at
        )


def read_case_screening_from_connection(
    connection: sqlite3.Connection,
    selection_id: str,
    *,
    subjects: tuple[str, ...],
    known_at: datetime,
    expected_generation_id: str | None = None,
) -> tuple[datetime, CaseScreening] | None:
    published = read_case_publication_from_connection(
        connection,
        selection_id=selection_id,
        known_at=known_at,
        expected_generation_id=expected_generation_id,
    )
    if published is None:
        return None
    published_at, digest, source = published
    used = set(subjects)
    result = source.result
    finite: ScreeningResult
    states: dict[tuple[str, str | None, str | None, bool | None], list[str]] = defaultdict(list)
    if isinstance(result, DiscoverySelection):
        # A planned row can explicitly have no models; absence of its identity is not equivalent.
        for entry in result.rows:
            states[
                entry["screening_lane"],
                entry["technical_state"],
                entry["financial_state"],
                entry["models_recorded"],
            ].append(entry["ticker"])
        finite = replace(result, rows=tuple(row for row in result.rows if row["ticker"] in used))
    elif isinstance(result, TechnicalFinancialSelection):
        for row in result.rows:
            states[
                row.screening_lane,
                _state(row.technical.get("state")),
                _state(row.financial_gate.get("state")),
                None,
            ].append(row.ticker)
        finite = replace(
            result,
            rows=tuple(row for row in result.rows if row.ticker in used),
            display_order=tuple(t for t in result.display_order if t in used),
        )
    elif isinstance(result, EconomicPathSelection):
        for card in result.cards:
            states["saved_card", None, None, None].append(card.ticker)
        finite = replace(result, cards=tuple(card for card in result.cards if card.ticker in used))
    else:
        for view in result.views:
            states[view.view_id, None, None, None].extend(row.ticker for row in view.companies)
        finite = replace(
            result,
            views=tuple(
                replace(view, companies=tuple(row for row in view.companies if row.ticker in used))
                for view in result.views
            ),
        )
    return published_at, CaseScreening(
        generation_id=source.generation_id,
        universe_tickers=source.universe_tickers,
        result=finite,
        price_inputs=source.price_inputs,
        source_sha256=digest,
        population_groups=tuple(
            PopulationGroup(scope, tech, fin, tuple(value), models)
            for (scope, tech, fin, models), value in states.items()
        ),
        legacy_candidate_count=None
        if isinstance(result, DiscoverySelection)
        else len(result.tickers),
    )


_CASE = TypeAdapter(CaseScreening)


def same_case_screening(saved: CaseScreening, canonical: CaseScreening) -> bool:
    # Python equality alone equates a true boolean with a numeric one in untyped evidence.
    return encode_json(_CASE.dump_python(saved, mode="json")) == encode_json(
        _CASE.dump_python(canonical, mode="json")
    )
