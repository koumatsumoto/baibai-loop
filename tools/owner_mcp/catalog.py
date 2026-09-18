"""調査工程へ利用可能な論理data入口をstoreに接続せず見せる。"""

from __future__ import annotations

from functools import partial
from typing import Any

from pydantic import BaseModel

from baibai_engine.market.lake.datasets import LAKE_DATASETS

from . import resource_models as m
from . import resources_application as app
from . import resources_macro as macro
from . import resources_market as market
from . import resources_screening as screen
from .resource_types import ResourceSpec


def resources() -> dict[str, ResourceSpec]:
    specs = [
        ResourceSpec(
            "macro.series",
            "L1",
            "macro",
            "Git registry",
            "全登録系列・aliases・source",
            m.SeriesKey,
            m.SeriesKey,
            m.SeriesFilters,
            ("series_id",),
            macro.series_get,
            macro.series_page,
        ),
        ResourceSpec(
            "macro.observation",
            "L1",
            "macro",
            "macro store",
            "effectiveまたは全vintageの保存履歴",
            m.ObservationKey,
            m.ObservationKey,
            m.ObservationFilters,
            ("observed_at", "vintage_at"),
            macro.observation_get,
            macro.observation_page,
            time_basis="observed_at / vintage_at",
        ),
        ResourceSpec(
            "macro.provider_run",
            "L1",
            "macro",
            "macro store",
            "取得状態。市場判断ではない",
            m.ProviderKey,
            m.ProviderKey,
            m.ProviderFilters,
            ("finished_at", "run_id"),
            macro.provider_get,
            macro.provider_page,
            time_basis="finished_at JST day",
        ),
        ResourceSpec(
            "macro.reading",
            "L2",
            "macro",
            "macro store + Git reading rules",
            "既存規則でread-time再計算。過去の保存snapshotではない",
            m.ReadingSelector,
            m.ReadingIdentity,
            m.ReadingSelector,
            ("series_id",),
            macro.reading_get,
            macro.reading_page,
            time_basis="as_of",
        ),
        ResourceSpec(
            "screening.run",
            "L2",
            "screening",
            "runs store",
            "保存run header。分析行は子resource",
            m.RunSelector,
            m.RunKey,
            m.DateRange,
            ("asof_date", "run_at", "run_revision_id"),
            screen.run_get,
            partial(screen.run_page, kind="screening_run"),
            "metadata",
            "as_of_date",
        ),
        ResourceSpec(
            "screening.security_analysis",
            "L2",
            "screening",
            "runs store",
            "Review Set外を含む全保存分析",
            m.AnalysisKey,
            m.AnalysisKey,
            m.AnalysisFilters,
            ("ordinal",),
            screen.analysis_get,
            partial(screen.run_page, kind="security_analysis"),
        ),
        ResourceSpec(
            "screening.review_set",
            "L2",
            "screening",
            "runs store",
            "full PublishedReviewSet",
            m.ReviewSelector,
            m.ReviewKey,
            m.ReviewFilters,
            ("asof_date", "created_at", "review_set_id"),
            screen.review_get,
            partial(screen.run_page, kind="review_set"),
            "metadata",
            "as_of",
        ),
        ResourceSpec(
            "screening.er_calibration_context",
            "L2",
            "screening",
            "production ER artifact",
            "保存値と現在の利用可否を分離",
            m.Empty,
            m.Empty,
            m.Empty,
            (),
            screen.er_get,
            None,
        ),
    ]
    calibration_specs: list[
        tuple[str, type[BaseModel], type[BaseModel], type[BaseModel], tuple[str, ...]]
    ] = [
        ("cohort", m.CohortSelector, m.CohortKey, m.CohortFilters, ("asof",)),
        ("panel_row", m.PanelSelector, m.PanelKey, m.PanelFilters, ("ticker",)),
        ("forward_row", m.ForwardSelector, m.ForwardKey, m.ForwardFilters, ("ticker", "horizon")),
    ]
    for name, selector, identity, filters, sort in calibration_specs:
        specs.append(
            ResourceSpec(
                f"screening.calibration.{name}",
                "L2",
                "screening",
                "calibration current",
                "保存較正結果。同じsnapshot_tokenで関連rowを固定",
                selector,
                identity,
                filters,
                sort,
                partial(screen.calibration_get, kind=name),
                partial(screen.calibration_page, kind=name),
            )
        )
    publication_specs: list[tuple[str, str, type[BaseModel], type[BaseModel], type[BaseModel]]] = [
        ("macro.context", "macro_context", m.ContextSelector, m.ContextKey, m.DateRange),
        ("research.triage", "research_triage", m.TriageKey, m.TriageKey, m.TriageFilters),
        ("research.thesis", "thesis", m.ThesisKey, m.ThesisKey, m.TickerRange),
        (
            "research.thesis_review",
            "thesis_review",
            m.ThesisReviewKey,
            m.ThesisReviewKey,
            m.ThesisReviewFilters,
        ),
        (
            "research.capital_allocation_assessment",
            "capital_allocation_assessment",
            m.AssessmentKey,
            m.AssessmentKey,
            m.DateRange,
        ),
        (
            "position.review",
            "position_review",
            m.PositionReviewKey,
            m.PositionReviewKey,
            m.TickerRange,
        ),
        ("portfolio.outcome", "portfolio_outcome", m.OutcomeKey, m.OutcomeKey, m.OutcomeFilters),
        ("task", "task", m.TaskKey, m.TaskKey, m.TaskFilters),
        (
            "operation.session",
            "operation_session",
            m.OperationKey,
            m.OperationKey,
            m.OperationFilters,
        ),
    ]
    for resource_id, table, selector, identity, filters in publication_specs:
        specs.append(
            ResourceSpec(
                resource_id,
                "L3",
                resource_id.split(".")[0],
                "application DB",
                "保存publication原本。現在の適格性を再判定しない",
                selector,
                identity,
                filters,
                app.TABLES[table][1],
                partial(app.publication_get, table=table),
                partial(app.publication_page, table=table),
                "metadata",
                "stored publication",
                f"{table} payload model",
            )
        )
    ledger_specs: list[tuple[str, str, type[BaseModel], type[BaseModel], tuple[str, ...]]] = [
        ("ledger", "ledger_meta", m.Empty, m.Empty, ()),
        ("ledger_event", "ledger_event", m.LedgerEventKey, m.LedgerEventFilters, ("append_seq",)),
        ("market_price", "ledger_market_price", m.PriceKey, m.TickerFilters, ("ticker",)),
    ]
    for name, kind, selector, filters, sort in ledger_specs:
        specs.append(
            ResourceSpec(
                f"portfolio.{name}",
                "L3",
                "portfolio",
                "application DB / human-confirmed facts",
                "保存台帳。時価評価・broker操作なし",
                selector,
                selector,
                filters,
                sort,
                partial(app.ledger_get, kind=kind),
                None if name == "ledger" else partial(app.ledger_page, kind=kind),
                time_basis="occurred_at JST day" if name == "ledger_event" else "stored",
            )
        )
    market_specs: list[tuple[str, str, type[BaseModel], type[BaseModel], tuple[str, ...]]] = [
        (
            "source_coverage",
            "source_coverage",
            m.CoverageKey,
            m.CoverageFilters,
            ("source", "coverage_key"),
        ),
        (
            "capital_policy_snapshot",
            "tse_capital_policy_snapshots",
            m.CapitalPolicyKey,
            m.TickerRange,
            ("snapshot_month_end", "ticker"),
        ),
    ]
    for name, kind, selector, filters, sort in market_specs:
        specs.append(
            ResourceSpec(
                f"market.{name}",
                "L1",
                "market",
                "market store-local",
                "lake対象外の保存fact",
                selector,
                selector,
                filters,
                sort,
                partial(market.market_get, kind=kind),
                partial(market.market_page, kind=kind),
            )
        )
    for name, dataset in LAKE_DATASETS.items():
        specs.append(
            ResourceSpec(
                f"market.{name}",
                "L1",
                "market",
                "R2 fixed lake release",
                "l1_resolve_current → l1_describe_dataset → l1_query",
                m.Empty,
                m.Empty,
                m.Empty,
                (),
                None,
                None,
                time_basis=dataset.date_column,
            )
        )
    return {spec.resource_id: spec for spec in specs}


def descriptor(spec: ResourceSpec, *, detail: bool = False) -> dict[str, Any]:
    lake = spec.get is None
    result: dict[str, Any] = {
        "resource_id": spec.resource_id,
        "layer": spec.layer,
        "domain": spec.domain,
        "authority": spec.authority,
        "operations": ["l1_resolve_current", "l1_describe_dataset", "l1_query"]
        if lake
        else ["data_get", *(["data_list"] if spec.page else [])],
        "list_shape": spec.list_shape,
        "identity_fields": list(spec.identity.model_fields),
        "time_basis": spec.time_basis,
        "description": spec.description,
    }
    if detail:
        result.update(
            selector_schema=spec.selector.model_json_schema(),
            filter_schema=spec.filters.model_json_schema(),
            identity_schema=spec.identity.model_json_schema(),
            sort=list(spec.sort),
            payload_schema=spec.payload_schema,
            examples=[{"resource_id": spec.resource_id}],
        )
        if spec.resource_id == "macro.series":
            from baibai_engine.macro.indicators.definitions import load_definitions

            result["series_ids"] = [item.series_id for item in load_definitions().series]
    return result
