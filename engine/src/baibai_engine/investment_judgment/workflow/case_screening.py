"""一度発行された探索から、Caseが実際に使った行と当時の母集団を固定する。"""

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Annotated

from pydantic import Field, TypeAdapter

from baibai_engine.machine_analysis.screening.contracts import ScreeningResult, ScreeningSelection
from baibai_engine.machine_analysis.screening.discovery_contract import (
    DISCOVERY_SUFFIX,
    DiscoverySelection,
    validate_discovery_publication,
)
from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathSelection
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialSelection,
)
from baibai_engine.machine_analysis.screening.selection_query import read_case_publication
from baibai_engine.primitives.json import encode_json


@dataclass(frozen=True, slots=True)
class PopulationGroup:
    """分類だけ。全社を実際に調査したとも、欠測を落選とも扱わない。"""

    scope: str
    technical_state: str | None
    financial_state: str | None
    tickers: tuple[str, ...]
    models_recorded: Annotated[bool | None, Field(exclude_if=lambda v: v is None)] = None


@dataclass(frozen=True, slots=True)
class CaseScreening(ScreeningSelection):
    """resultの明細は有限行のみ。全体の明細はsource_sha256が指す発行物に属する。"""

    source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] = field(kw_only=True)
    population_groups: tuple[PopulationGroup, ...] = field(kw_only=True)
    legacy_candidate_count: Annotated[int | None, Field(ge=0, exclude_if=lambda v: v is None)] = (
        field(kw_only=True, default=None)
    )


def _state(value: object) -> str | None:
    return value if isinstance(value, str) else None


def read_case_screening(
    selection_id: str, *, subjects: tuple[str, ...], known_at: datetime, path: Path
) -> tuple[datetime, CaseScreening] | None:
    published = read_case_publication(path=path, selection_id=selection_id, known_at=known_at)
    if published is None:
        return None
    published_at, digest, source = published
    used = set(subjects)
    result = source.result
    finite: ScreeningResult
    states: dict[tuple[str, str | None, str | None, bool | None], list[str]] = defaultdict(list)
    if selection_id.endswith(DISCOVERY_SUFFIX) != isinstance(result, DiscoverySelection):
        raise ValueError("discovery publication identity differs from its format")
    if isinstance(result, DiscoverySelection):
        validate_discovery_publication(
            selection_id=selection_id,
            generation_id=source.generation_id,
            result=result,
            universe_tickers=source.universe_tickers,
        )
        # A planned row can explicitly have no models; absence of its identity is not equivalent.
        for entry in result.rows:
            states[
                entry["screening_lane"],
                entry["technical_state"],
                entry["financial_state"],
                entry["models_recorded"],
            ].append(entry["ticker"])
        finite = replace(
            result, rows=tuple(entry for entry in result.rows if entry["ticker"] in used)
        )
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
