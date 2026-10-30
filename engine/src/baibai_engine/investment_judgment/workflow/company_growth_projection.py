"""保存済み成長結果をL3へ薄く表示する。取得・財務再計算・保存入力変更はしない。"""

from __future__ import annotations

from typing import cast

from pydantic import JsonValue

from baibai_engine.machine_analysis.models.company_growth.models import (
    CompanyGrowth,
    Metric,
    Observation,
    PeriodState,
    SourceDocument,
)

_METRICS: tuple[Metric, ...] = (
    "sales",
    "operating_profit",
    "parent_profit",
    "operating_margin",
    "reported_basic_eps",
)


def project_company_growth(
    saved: JsonValue, *, ticker: str, periods: int | None = 12
) -> dict[str, JsonValue]:
    if periods is not None and periods < 1:
        raise ValueError("company growth display periods must be positive or all")
    result = CompanyGrowth.model_validate(saved)
    shown = result.periods if periods is None else result.periods[-periods:]
    documents: dict[SourceDocument, str] = {}

    def references(sources: tuple[SourceDocument, ...]) -> list[str]:
        return [documents.setdefault(source, f"d{len(documents) + 1}") for source in sources]

    def project_row(
        period: PeriodState, observation: Observation, names: tuple[Metric, ...]
    ) -> dict[str, object]:
        metrics = {name: observation.metrics[name] for name in names}
        sources = tuple(dict.fromkeys(d for value in metrics.values() for d in value.documents))
        row: dict[str, object] = {
            "period_start": observation.period_start.isoformat(),
            "period_end": observation.period_end.isoformat(),
            "fiscal_period": period.fiscal_period,
            "fiscal_year_end": None
            if period.fiscal_year_end is None
            else period.fiscal_year_end.isoformat(),
            "statement_basis": observation.statement_basis,
            "values": [value.value for value in metrics.values()],
            "sources": references(sources),
        }
        if observation.accounting_standard is not None:
            row["accounting_standard"] = observation.accounting_standard
        if observation.period_status is not None:
            row["period_status"] = observation.period_status
        missing = {name: value.reason for name, value in metrics.items() if value.reason}
        if missing:
            row["missing"] = missing
        if any(value.documents != sources for value in metrics.values()):
            row["metric_sources"] = {
                name: references(value.documents) for name, value in metrics.items()
            }
        return row

    rows = [
        project_row(period, observation, _METRICS)
        for period in shown
        for observation in period.observations
        if observation.measurement == "reported"
    ]
    # Only the newest saved period is summarized. Do not search backwards for a
    # non-null result, derive a new TTM, or promote old balances to the current date.
    latest = shown[-1] if shown else None
    latest_observations: dict[str, object] = {}
    if latest is not None:
        for observation in latest.observations:
            if observation.measurement == "reported":
                continue
            names = tuple(observation.metrics)
            latest_observations[observation.measurement] = {
                "columns": list(names),
                "units": {name: result.metric_units[name] for name in names},
                "share_basis": observation.share_basis,
                "row": project_row(latest, observation, names),
                "yoy": [
                    change.model_dump(mode="json")
                    for change in result.changes
                    if change.measurement == observation.measurement
                    and change.to_start == observation.period_start
                    and change.to_end == observation.period_end
                ],
            }
    cash_flow_rows = [
        project_row(period, observation, ("operating_cash_flow",))
        for period in shown
        for observation in period.observations
        if observation.measurement == "reported"
    ]
    shown_periods = {(period.period_start, period.period_end) for period in shown}
    changes = [
        [
            change.to_start.isoformat(),
            change.to_end.isoformat(),
            change.metric,
            None if change.from_start is None else change.from_start.isoformat(),
            None if change.from_end is None else change.from_end.isoformat(),
            change.from_value,
            change.difference,
            change.rate,
            change.margin_points,
            change.sign_change,
            change.reason,
        ]
        for change in result.changes
        if (change.to_start, change.to_end) in shown_periods
        and change.measurement == "reported"
        and change.metric in _METRICS[:-1]
    ]
    payload = result.model_dump(mode="json")
    return cast(
        dict[str, JsonValue],
        {
            **{
                key: payload[key]
                for key in (
                    "ticker",
                    "status",
                    "reason",
                    "calculation_method",
                    "read_semantics",
                    "comparison_basis",
                    "input_release",
                    "per",
                    "peer",
                )
            },
            "full_result_ref": f"/inputs/securities_context/{ticker}/company_growth",
            "saved_scope": {
                "requested_start": payload["requested_start"],
                "requested_end": payload["requested_end"],
                "period_count": len(result.periods),
                "first_period_end": None
                if not result.periods
                else result.periods[0].period_end.isoformat(),
                "last_period_end": None
                if not result.periods
                else result.periods[-1].period_end.isoformat(),
            },
            "display_scope": {
                "latest_period_limit": periods,
                "period_count": len(shown),
                "first_period_end": None if not shown else shown[0].period_end.isoformat(),
                "last_period_end": None if not shown else shown[-1].period_end.isoformat(),
            },
            "coverage": result.coverage.model_dump(mode="json", exclude={"observed_period_ends"}),
            "reported": {
                "measurement": "reported_FY_or_year_to_date_not_single_quarter",
                "share_basis": "source_reported_unverified",
                "columns": list(_METRICS),
                "units": {name: result.metric_units[name] for name in _METRICS},
                "rows": rows,
            },
            "reported_yoy": {
                "columns": [
                    "to_start",
                    "to_end",
                    "metric",
                    "from_start",
                    "from_end",
                    "from_value",
                    "difference",
                    "rate",
                    "margin_points",
                    "sign_change",
                    "reason",
                ],
                "to_values_ref": "reported.rows by period_start/period_end and metric column",
                "rows": changes,
            },
            "latest_observations": latest_observations,
            "reported_cash_flow": {
                "measurement": "reported_FY_or_year_to_date_not_single_quarter",
                "columns": ["operating_cash_flow"],
                "units": {"operating_cash_flow": result.metric_units["operating_cash_flow"]},
                "rows": cash_flow_rows,
            },
            "documents": {ref: source.model_dump(mode="json") for source, ref in documents.items()},
            "other_measurements_ref": "full_result_ref: periods.observations and changes",
        },
    )
