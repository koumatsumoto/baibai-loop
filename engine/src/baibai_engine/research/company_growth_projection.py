"""保存済み成長結果をL3へ薄く表示する。取得・財務再計算・保存入力変更はしない。"""

from __future__ import annotations

from typing import cast

from pydantic import JsonValue

from baibai_engine.company_growth.models import CompanyGrowth, Metric, SourceDocument

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

    rows: list[dict[str, object]] = []
    for period in shown:
        for observation in period.observations:
            if observation.measurement != "reported":
                continue
            metrics = {name: observation.metrics[name] for name in _METRICS}
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
            missing = {name: value.reason for name, value in metrics.items() if value.reason}
            if missing:
                row["missing"] = missing
            if any(value.documents != sources for value in metrics.values()):
                row["metric_sources"] = {
                    name: references(value.documents) for name, value in metrics.items()
                }
            rows.append(row)
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
            "documents": {ref: source.model_dump(mode="json") for source, ref in documents.items()},
            "other_measurements_ref": "full_result_ref: periods.observations and changes",
        },
    )
