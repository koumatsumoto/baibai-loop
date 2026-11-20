"""保存済み入力の選定contextと初回照合。取得・書込・AI起動は行わない。"""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import date, datetime
from math import isfinite
from pathlib import Path
from typing import Any, cast

from pydantic import TypeAdapter

from baibai_engine.investment_judgment.company_research.context import company_context_brief
from baibai_engine.investment_judgment.company_research.query import read_company_contexts
from baibai_engine.investment_judgment.judgment_retrospective.holdings import holding_review_history
from baibai_engine.investment_judgment.market_assessment.projection import assessment_projection
from baibai_engine.investment_judgment.market_assessment.query import latest_assessment
from baibai_engine.investment_judgment.workflow.book_prices import read_book_prices
from baibai_engine.investment_judgment.workflow.discovery import (
    PriceCoordinateMinimum,
    discovery_context,
)
from baibai_engine.investment_judgment.workflow.screening_brief import (
    SCREENING_EXCLUSION_SCOPES,
    ScreeningExclusionScope,
    economic_screening_brief,
    technical_financial_exclusion_diagnostic,
    technical_financial_screening_brief,
)
from baibai_engine.investment_judgment.workflow.selection import ResearchRequest
from baibai_engine.investment_judgment.workflow.selection_policy import research_capacity
from baibai_engine.machine_analysis.generation.contracts import ModelInputSlice
from baibai_engine.machine_analysis.screening.contracts import (
    ScreeningResult,
    ScreeningSelection,
)
from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathSelection
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    ResearchLane,
    TechnicalFinancialSelection,
)
from baibai_engine.portfolio.ledger.events import replay_events_through
from baibai_engine.portfolio.ledger.read import load_ledger_document
from baibai_engine.primitives.stocks import normalize_ticker, sector_33_peer_key
from baibai_engine.primitives.time import JST
from baibai_engine.storage.sqlite.application.paths import database_path
from baibai_engine.storage.sqlite.application.read import connect_read_only


def load_generated_selection_context(
    *,
    request: ResearchRequest,
    generation_id: str | None,
    screening_selection_id: str | None,
    research_as_of: date | None,
    known_at: datetime,
    runs_path: Path,
    db_path: Path | None,
    comparison_tickers: tuple[str, ...] = (),
    screening_page: int = 0,
    screening_lane: ResearchLane | None = None,
    discovery_question: str | None = None,
    price_coordinate_min: PriceCoordinateMinimum | None = None,
    reported_pbr_max: float | None = None,
    screening_excluded: bool = False,
    screening_excluded_scope: ScreeningExclusionScope = "technical_matched",
) -> dict[str, Any]:
    """保存された政策結果と同じ生成を読む。contextの取得で再選定しない。"""
    from baibai_engine.machine_analysis.generation.contracts import research_subjects
    from baibai_engine.machine_analysis.generation.query import (
        read_model_inputs,
        read_research_model_inputs,
    )
    from baibai_engine.machine_analysis.screening.discovery_index import read_discovery_index
    from baibai_engine.machine_analysis.screening.selection_query import read_publication

    if known_at.utcoffset() is None:
        raise ValueError("input cutoff must have a timezone")
    if screening_page < 0:
        raise ValueError("screening page must be nonnegative")
    if screening_excluded_scope not in SCREENING_EXCLUSION_SCOPES or (
        not screening_excluded and screening_excluded_scope != "technical_matched"
    ):
        raise ValueError("excluded diagnostic scope requires an explicit valid diagnostic")
    if screening_excluded and (request.scope != "discover_new" or screening_lane is not None):
        raise ValueError("excluded diagnostic requires discovery and no lane override")
    archived_view = screening_lane is not None or screening_excluded
    if archived_view and screening_selection_id is None:
        raise ValueError("archived screening requires an explicit legacy screening selection")
    as_of = research_as_of or known_at.astimezone(JST).date()
    if as_of > known_at.astimezone(JST).date():
        raise ValueError("Research date/cutoff is invalid")
    overview = request.scope == "discover_new" and not archived_view
    if reported_pbr_max is not None and (
        discovery_question != "reported_assets"
        or price_coordinate_min is not None
        or isinstance(reported_pbr_max, bool)
        or not isfinite(reported_pbr_max)
        or reported_pbr_max <= 0
    ):
        raise ValueError(
            "reported PBR needs a positive finite maximum and only the reported_assets question"
        )
    if price_coordinate_min is not None and discovery_question is None:
        raise ValueError("a price-coordinate comparison needs an explicit discovery question")
    if discovery_question is not None and (not overview or comparison_tickers):
        raise ValueError("discovery question requires new discovery without other views")
    if overview and screening_page and discovery_question is None:
        raise ValueError("choose a discovery question before paging")
    if (
        request.scope == "discover_new"
        and generation_id is not None
        and screening_selection_id is None
    ):
        raise ValueError("discovery requires an explicit saved screening selection")
    # 最新は一度だけ解決する。旧本文を読む権限を暗黙の最新IDに引き継がない。
    legacy_targeted_read = False
    if screening_selection_id is not None or generation_id is None:
        index = read_discovery_index(
            path=runs_path, selection_id=screening_selection_id, as_of=as_of, known_at=known_at
        )
        if index.status == "no_publication" and screening_selection_id is not None:
            raise ValueError("unknown or not yet published screening selection")
        if generation_id is not None and index.generation_id != generation_id:
            raise ValueError("screening selection differs from the chosen generation")
        if index.status == "indexed" and archived_view:
            raise ValueError(
                "archived screening requires a legacy publication; use --discovery-question"
            )
        if index.status != "unindexed_legacy" or (
            overview and (not comparison_tickers or screening_selection_id is None)
        ):
            app = database_path(db_path)
            held, history = (
                _ledger_positions(app, known_at, as_of, request.requested_tickers)
                if index.status == "indexed" and request.scope == "review_positions"
                else (None, None)
            )
            requested = request.requested_tickers or held or ()
            comparisons = tuple(
                sorted(set(requested) | {normalize_ticker(t) for t in comparison_tickers})
            )
            detail = bool(comparison_tickers) or request.scope != "discover_new"
            if detail and screening_page:
                raise ValueError("choose explicit company detail or one discovery question page")
            book_prices = None
            if index.status == "indexed" and reported_pbr_max is not None:
                assert index.generation_id is not None
                assert index.as_of is not None
                book_prices = read_book_prices(
                    generation_id=index.generation_id,
                    as_of=index.as_of,
                    maximum=reported_pbr_max,
                    path=runs_path,
                    listings=index.listing_dates(),
                )
            indexed = discovery_context(
                index,
                request=request,
                as_of=as_of,
                known_at=known_at,
                path=runs_path,
                db_path=db_path,
                minimum=price_coordinate_min,
                book_prices=book_prices,
                question=discovery_question,
                page=screening_page,
                comparisons=comparisons if detail else None,
            )
            if indexed["status"] != "ready" or not detail:
                return indexed
            assert index.generation_id is not None
            models = read_research_model_inputs(
                index.generation_id, targets=comparisons, path=runs_path
            )
            if models is None:
                raise ValueError("chosen model generation is unavailable")
            _validate_model_cutoff(models, as_of, known_at)
            indexed["model_inputs"] = _model_context(models, set(comparisons), compact=True)
            indexed.update(_judgments(app, comparisons, as_of, known_at))
            indexed["requested_tickers"] = requested
            if request.scope == "review_positions":
                indexed["capacity"] = research_capacity(request.scope, len(held or ())).model_dump(
                    mode="json"
                )
                indexed["holding_review_history"] = history
                indexed["held_tickers"] = held
                indexed["holding_scope"] = (
                    "no_ledger_record" if held is None else "current_ledger_replayed_to_fact_cutoff"
                )
            return indexed
        generation_id = index.generation_id
        legacy_targeted_read = overview and bool(comparison_tickers)
    lane = screening_lane or "growth_research"
    publication = None
    screening = None
    if screening_selection_id is not None:
        publication = read_publication(
            path=runs_path, selection_id=screening_selection_id, as_of=as_of, known_at=known_at
        )
        if publication is None:
            raise ValueError("unknown or not yet published screening selection")
        screening = publication.selection
        if screening.generation_id != generation_id:
            raise ValueError("screening selection differs from the chosen generation")
    if generation_id is None:
        raise ValueError("an explicit generation is required")
    app_path = database_path(db_path)
    ledger_positions, history = _ledger_positions(
        app_path,
        known_at,
        as_of if request.scope == "review_positions" else None,
        request.requested_tickers,
    )
    held = ledger_positions or ()
    requested = request.requested_tickers or (held if request.scope == "review_positions" else ())
    candidates = () if screening is None else screening.result.tickers
    technical_screening = screening is not None and isinstance(
        screening.result, TechnicalFinancialSelection
    )
    if technical_screening:
        assert screening is not None
        assert isinstance(screening.result, TechnicalFinancialSelection)
        start = screening_page * screening.result.policy.page_size
        candidates = (
            ()
            if screening_excluded or legacy_targeted_read
            else screening.result.lane_tickers(lane)[
                start : start + screening.result.policy.page_size
            ]
        )
    elif screening_page or lane != "growth_research" or screening_excluded:
        raise ValueError("screening pagination requires a technical-financial selection")
    compact_screening = screening is not None and isinstance(
        screening.result, (EconomicPathSelection, TechnicalFinancialSelection)
    )
    detail_targets = set(requested) | {normalize_ticker(ticker) for ticker in comparison_tickers}
    if compact_screening:
        # 候補株の詳細を展開せず、発行時の完全planから候補に必要な共有文脈を解く。
        # 明示対象・保有は同じgenerationの全予定モデルを通常どおり読める。
        assert publication is not None
        if request.scope == "review_positions":
            detail_targets.update(held)
        metadata = publication.generation
        related = research_subjects(metadata, tuple(sorted(detail_targets | set(candidates))))
        plan = metadata.subject_plan
        shared = (
            {"market"}
            if plan is None
            else {subject for subject, definition in plan.items() if definition.scope != "stock"}
        )
        subjects = tuple(
            subject for subject in related if subject in detail_targets or subject in shared
        )
        models = read_model_inputs(generation_id, subjects=subjects, path=runs_path)
    else:
        models = read_research_model_inputs(
            generation_id, targets=tuple(sorted(detail_targets | set(candidates))), path=runs_path
        )
    if models is None:
        raise ValueError("chosen model generation is unavailable")
    _validate_model_cutoff(models, as_of, known_at)
    model_inputs = _model_context(models, detail_targets, compact=compact_screening)
    return {
        "schema_version": 3,
        "status": "ready",
        **(
            {"read_cost": "explicit_full_saved_publication"}
            if request.scope == "discover_new"
            else {}
        ),
        "request": request.model_dump(mode="json"),
        "research_as_of": as_of.isoformat(),
        "input_known_at": known_at.isoformat(),
        "generation_id": generation_id,
        "screening_selection_id": screening_selection_id,
        "capacity": research_capacity(request.scope, len(held)).model_dump(mode="json"),
        "screening": None
        if screening is None
        else _project_screening(
            screening,
            page=None if legacy_targeted_read and technical_screening else screening_page,
            lane=lane,
            comparison_tickers=tuple(sorted(detail_targets)),
            excluded=screening_excluded,
            excluded_scope=screening_excluded_scope,
        ),
        "candidate_tickers": list(candidates),
        "candidate_order_semantics": (
            "diagnostic_rows_are_not_candidate_tickers"
            if screening_excluded
            else "saved_route_round_robin_page_not_investment_ranking"
            if technical_screening
            else "ticker_code_order_attention_priority_in_screening_cards_rank"
            if compact_screening
            else "per_view_rank_not_a_union_investment_ranking"
        ),
        "requested_tickers": requested,
        "held_tickers": held,
        **(
            {
                "holding_review_history": history,
                "holding_scope": (
                    "no_ledger_record"
                    if ledger_positions is None
                    else "current_ledger_replayed_to_fact_cutoff"
                ),
            }
            if request.scope == "review_positions"
            else {}
        ),
        "model_inputs": model_inputs,
        **_judgments(app_path, tuple(sorted(set(candidates) | detail_targets)), as_of, known_at),
    }


def _judgments(
    app: Path, tickers: tuple[str, ...], as_of: date, known_at: datetime
) -> dict[str, Any]:
    market = latest_assessment(app, as_of=as_of, known_at=known_at)
    return {
        "saved_company_context": {
            ticker: company_context_brief(item)
            for ticker, item in read_company_contexts(
                app, tickers=tickers, as_of=as_of, known_at=known_at
            ).items()
        },
        "saved_market_context_assessment": None
        if market is None
        else assessment_projection(market, as_of=as_of),
    }


def _ledger_positions(
    app_path: Path, known_at: datetime, review_as_of: date | None, requested: tuple[str, ...]
) -> tuple[tuple[str, ...] | None, dict[str, object] | None]:
    if not app_path.exists():
        return None, None
    with closing(connect_read_only(app_path)) as connection:
        connection.execute("BEGIN")
        ledger = load_ledger_document(connection)
        if ledger is None:
            return None, None
        state = replay_events_through(ledger.events, known_at)
        held = tuple(
            sorted(t for t, lots in state.lots.items() if any(lot.quantity > 0 for lot in lots))
        )
        if review_as_of is None:
            return held, None
        selected = set(held).intersection(requested) if requested else set(held)
        return held, holding_review_history(
            connection, ledger, selected, as_of=review_as_of, known_at=known_at
        )


def _validate_model_cutoff(models: ModelInputSlice, as_of: date, known_at: datetime) -> None:
    if models.as_of > as_of or models.evaluation_time > known_at:
        raise ValueError("chosen generation is after the research cutoff")


def _withhold_unclassified_sector_peer(payload: dict[str, Any]) -> bool:
    master = payload.get("master")
    sector = master.get("sector_33") if isinstance(master, dict) else None
    relative = payload.get("relative")
    if sector_33_peer_key(sector) is not None or not isinstance(relative, dict):
        return False
    if not isinstance(relative.get("sector"), dict):
        return False
    # 旧保存に架空の「その他同業」があっても、比較未成立を相対騰落にしない。
    relative["sector"] = None
    relative["sector_unavailable_reason"] = "unclassified_sector"
    return True


def _model_context(
    models: ModelInputSlice, detail_targets: set[str], *, compact: bool
) -> dict[str, Any]:
    from baibai_engine.investment_judgment.workflow.generated_inputs import project_generated_inputs

    model_inputs = cast(
        dict[str, Any],
        project_generated_inputs(
            models,
            company_growth_periods=None if compact else 12,
            full_growth_tickers=detail_targets,
        ),
    )
    # Selection未保存のcontextは、現存する固定generationを参照する。
    # prepare後の投影だけがapplication DB内のSelectionを参照できる。
    for name, identity in models.inputs.items():
        if identity.kind == "native_l1":
            entry = model_inputs["analysis_source"]["inputs"][name]
            entry["generation_input_ref"] = {
                "generation_id": models.generation_id,
                "json_pointer": entry.pop("full_result_ref").removeprefix("/inputs/generation"),
            }
    for model in models.models:
        if model.model_id == "company_growth" and model.subject in detail_targets:
            # 明示詳細は完全な原文で、省略した表示への参照はない。
            continue
        if model.model_id not in {"company_financials", "company_growth", "market_context"}:
            continue
        if models.subject_plan is None:
            payload = (
                model_inputs["market_context"]
                if model.subject == "market"
                else model_inputs["securities"][model.subject][model.model_id]
            )
        else:
            group = (
                "securities"
                if models.subject_plan[model.subject].scope == "stock"
                else "shared_subjects"
            )
            payload = model_inputs[group][model.subject]["models"][model.model_id]
        projected = payload.pop("full_result_ref", None) is not None
        if model.model_id == "market_context" and model.subject in detail_targets:
            projected |= _withhold_unclassified_sector_peer(payload)
        if projected:
            # A slice index need not match the full Generation's model order.
            payload["generation_model_ref"] = {
                "generation_id": models.generation_id,
                "subject": model.subject,
                "model_id": model.model_id,
            }
    return model_inputs


def _project_screening(
    selection: ScreeningSelection,
    *,
    page: int | None = 0,
    lane: ResearchLane = "growth_research",
    comparison_tickers: tuple[str, ...] = (),
    excluded: bool = False,
    excluded_scope: ScreeningExclusionScope = "technical_matched",
) -> dict[str, Any]:
    result = selection.result
    if isinstance(result, TechnicalFinancialSelection):
        projection = technical_financial_screening_brief(
            result,
            page=None if excluded else page,
            lane=lane,
            comparison_tickers=comparison_tickers,
        )
        if excluded:
            projection["excluded_diagnostic"] = technical_financial_exclusion_diagnostic(
                result, page=0 if page is None else page, scope=excluded_scope
            )
    else:
        adapter = TypeAdapter[ScreeningResult](ScreeningResult)
        projection = cast(dict[str, Any], adapter.dump_python(result, mode="json"))
        if isinstance(result, EconomicPathSelection):
            projection = economic_screening_brief(projection)
    if selection.price_inputs is not None:
        projection["price_inputs"] = deepcopy(selection.price_inputs)
    return projection
