"""Financial disclosure decoding and point-in-time forecast projection; no I/O."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, time
from typing import Any

from baibai_engine.market.jquants_decode import (
    JQuantsProviderError,
    has_period_slot,
    parse_date,
    parse_jquants_code_parts,
    parse_optional_date,
    provider_record_payload,
    to_float,
    to_period,
)
from baibai_engine.market.jquants_models import JQuantsFinancialSummary


def normalize_financial_summary(record: Mapping[str, Any]) -> JQuantsFinancialSummary | None:
    """Decode native provider fields; select forecast period and basis only in L2 projection."""
    ticker, common_code = parse_jquants_code_parts(record.get("Code"))
    if not common_code:
        return None
    return JQuantsFinancialSummary(
        ticker=ticker,
        disclosed_at=parse_date(record.get("DiscDate")),
        eps_ttm=to_float(record.get("EPS")),
        bps=to_float(record.get("BPS")),
        shares_outstanding=to_float(record.get("ShOutFY")),
        sales=to_float(record.get("Sales")),
        cfo=to_float(record.get("CFO")),
        cash_eq=to_float(record.get("CashEq")),
        total_assets=to_float(record.get("TA")),
        equity=to_float(record.get("Eq")),
        operating_profit=to_float(record.get("OP")),
        ordinary_profit=to_float(record.get("OdP")),
        profit=to_float(record.get("NP")),
        fiscal_period=to_period(record.get("CurPerType")),
        fiscal_year_end=parse_optional_date(record.get("CurFYEn")),
        period_start=parse_optional_date(record.get("CurPerSt")),
        period_end=parse_optional_date(record.get("CurPerEn")),
        dps_actual_annual=to_float(record.get("DivAnn")),
        treasury_shares=to_float(record.get("TrShFY")),
        equity_to_asset_ratio=to_float(record.get("EqAR")),
        dividend_q1=to_float(record.get("Div1Q")),
        dividend_interim=to_float(record.get("Div2Q")),
        dividend_q3=to_float(record.get("Div3Q")),
        dividend_year_end=to_float(record.get("DivFY")),
        dividend_total_annual=to_float(record.get("DivTotalAnn")),
        average_shares=to_float(record.get("AvgSh")),
        disclosure_number=required_disclosure_number(record),
        disclosed_time=_disclosed_time(record.get("DiscTime")),
        document_type=_text(record.get("DocType")),
        document_payload=provider_record_payload(record),
    )


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return None if text in {"", "nan", "NaN", "NaT", "<NA>"} else text


def _disclosed_time(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = time.fromisoformat(text)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.tzinfo is None else None


def required_disclosure_number(record: Mapping[str, Any]) -> str:
    """Use the native disclosure identity; a content digest is not a disclosure number."""
    number = _text(record.get("DiscNo"))
    if number is None:
        raise JQuantsProviderError("financial source record has no native DiscNo")
    return number


def disclosure_revision(row: JQuantsFinancialSummary) -> tuple[date, str]:
    """Presentation key only. Unknown times never establish revision precedence."""
    return row.disclosed_at, row.disclosed_time or ""


def latest_disclosure_candidates(
    rows: Sequence[JQuantsFinancialSummary],
) -> list[JQuantsFinancialSummary]:
    """Keep same-day unknown times alongside the latest known-time candidates."""
    if not rows:
        return []
    day = max(row.disclosed_at for row in rows)
    same_day = [row for row in rows if row.disclosed_at == day]
    known_time = max((row.disclosed_time for row in same_day if row.disclosed_time), default=None)
    return [row for row in same_day if not row.disclosed_time or row.disclosed_time == known_time]


def statement_basis(row: JQuantsFinancialSummary) -> str:
    kind = row.document_type or ""
    if "NonConsolidated" in kind:
        return "nonconsolidated"
    if "_Consolidated_" in kind:
        return "consolidated"
    return "primary_unknown"


def _statement_basis_state(rows: Sequence[JQuantsFinancialSummary]) -> str:
    if not rows:
        return "primary_unknown"
    end = max(row.fiscal_year_end or date.min for row in rows)
    candidates = latest_disclosure_candidates(
        [row for row in rows if (row.fiscal_year_end or date.min) == end]
    )
    bases = {statement_basis(row) for row in candidates}
    return next(iter(bases)) if len(bases) == 1 else "basis_unresolved"


def _primary_source_basis(
    row: JQuantsFinancialSummary,
    statements: Sequence[JQuantsFinancialSummary],
) -> str:
    if "FinancialStatements" in (row.document_type or ""):
        return statement_basis(row)
    previous = [s for s in statements if s.disclosed_at < row.disclosed_at]
    same_day = [s for s in statements if s.disclosed_at == row.disclosed_at]
    certain = [
        s
        for s in same_day
        if s.disclosed_time and row.disclosed_time and s.disclosed_time <= row.disclosed_time
    ]
    uncertain = [s for s in same_day if not s.disclosed_time or not row.disclosed_time]
    basis = _statement_basis_state([*previous, *certain])
    alternatives = {basis, *(statement_basis(s) for s in uncertain)}
    return basis if len(alternatives) == 1 else "primary_unknown"


def _payload(row: JQuantsFinancialSummary) -> dict[str, Any]:
    return json.loads(row.document_payload) if row.document_payload else {}


def _period(record: Mapping[str, Any], slot: str) -> tuple[date, date] | None:
    prefix = "Cur" if slot == "current" else "Nxt"
    start = parse_optional_date(record.get(f"{prefix}FYSt"))
    end = parse_optional_date(record.get(f"{prefix}FYEn"))
    return (start, end) if start is not None and end is not None and start <= end else None


def _annual_earnings_revision(record: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
    """An explicit revision with no usable annual value must not revive old guidance.

    Half-year-only revisions leave annual guidance untouched. Empty annual revisions
    may mean an undecided forecast or a range, not necessarily a withdrawal.
    """
    if record.get("DocType") != "EarnForecastRevision":
        return False
    annual_keys = (*keys, "FNCSales", "FNCOP") if keys[0] == "FNCEPS" else (*keys, "FSales", "FOP")
    half_year_keys = (
        "FSales2Q", "FOP2Q", "FOdP2Q", "FNP2Q", "FEPS2Q",
        "FNCSales2Q", "FNCOP2Q", "FNCOdP2Q", "FNCNP2Q", "FNCEPS2Q",
    )
    return any(has_period_slot(record, key) for key in annual_keys) or not any(
        has_period_slot(record, key) for key in half_year_keys
    )


def project_financial_documents(
    summaries: Sequence[JQuantsFinancialSummary], asof_date: date
) -> list[JQuantsFinancialSummary]:
    """Project each issuer's applicable period without changing actual facts.

    Choose fiscal-period state before looking for numeric guidance. An empty new
    period cannot revive an older period's forecast. Earnings and DPS are independent
    document streams; the three earnings fields stay in one slot and one basis.
    Raw decoding never substitutes a forecast from another period when metadata is
    absent or invalid. Actual observations remain available independently of guidance.
    """
    by_ticker: dict[str, list[JQuantsFinancialSummary]] = {}
    for row in summaries:
        if row.disclosed_at <= asof_date:
            by_ticker.setdefault(row.ticker, []).append(row)
    projected: list[JQuantsFinancialSummary] = []
    for rows in by_ticker.values():
        records = [_payload(row) for row in rows]
        known = [i for i, record in enumerate(records) if _period(record, "current")]
        if not known:
            projected.extend(rows)
            continue
        # A later revision of an old accounting period is not a new fiscal state.
        states: list[tuple[int, tuple[date, date]]] = []
        for i in known:
            for slot in ("current", "next"):
                period = _period(records[i], slot)
                if period is not None and period[0] <= asof_date:
                    states.append((i, period))
        latest_start = max((period[0] for _, period in states), default=None)
        state_rows = latest_disclosure_candidates(
            [rows[i] for i, period in states if period[0] == latest_start]
        )
        targets = {
            period for i, period in states if period[0] == latest_start and rows[i] in state_rows
        }
        target = next(iter(targets)) if len(targets) == 1 else None
        if target is not None and target[1] < asof_date:
            target = None
        statements = [row for row in rows if "FinancialStatements" in (row.document_type or "")]
        target_basis = _statement_basis_state(statements)
        for i, row in enumerate(rows):
            record = records[i]
            selected_slot = next(
                (
                    slot
                    for slot in ("current", "next")
                    if target is not None and _period(record, slot) == target
                ),
                None,
            )
            field_sources: list[tuple[str, tuple[str, ...]]] = []
            source_basis = _primary_source_basis(row, statements)
            eps = profit = ordinary = dps = None
            earnings_status = dividend_status = "not_applicable"
            if selected_slot is not None:
                prefix = "F" if selected_slot == "current" else "NxF"
                primary_keys = (
                    f"{prefix}EPS",
                    "FNP" if selected_slot == "current" else "NxFNp",
                    f"{prefix}OdP",
                )
                nc_keys = (f"{prefix}NCEPS", f"{prefix}NCNP", f"{prefix}NCOdP")
                has_primary = any(has_period_slot(record, key) for key in primary_keys)
                has_nc = any(has_period_slot(record, key) for key in nc_keys)
                use_nc = has_nc and (target_basis == "nonconsolidated" or not has_primary)
                keys = nc_keys if use_nc else primary_keys
                source_basis = "nonconsolidated" if use_nc else source_basis
                if source_basis != target_basis or target_basis == "basis_unresolved":
                    earnings_status = "basis_mismatch"
                else:
                    earnings_status = (
                        "confirmed"
                        if any(has_period_slot(record, key) for key in keys)
                        else "missing"
                    )
                    unresolved_revision = (
                        selected_slot == "current"
                        and earnings_status == "missing"
                        and _annual_earnings_revision(record, keys)
                    )
                    if unresolved_revision:
                        earnings_status = "unresolved"
                    eps, profit, ordinary = (to_float(record.get(key)) for key in keys)
                    for field, key in zip(
                        ("forecast_eps", "forecast_profit", "forecast_ordinary_profit"),
                        keys,
                        strict=True,
                    ):
                        if (has_period_slot(record, key) or unresolved_revision) and row.disclosure_number:
                            field_sources.append((field, (row.disclosure_number,)))
                dps_key = f"{prefix}DivAnn"
                dividend_status = "confirmed" if has_period_slot(record, dps_key) else "missing"
                unresolved_dividend_revision = (
                    selected_slot == "current"
                    and dividend_status == "missing"
                    and row.document_type == "DividendForecastRevision"
                )
                if unresolved_dividend_revision:
                    dividend_status = "unresolved"
                dps = to_float(record.get(dps_key))
                if (has_period_slot(record, dps_key) or unresolved_dividend_revision) and row.disclosure_number:
                    field_sources.append(("dps_forecast_annual", (row.disclosure_number,)))
            projected.append(
                replace(
                    row,
                    forecast_eps=eps,
                    forecast_profit=profit,
                    forecast_ordinary_profit=ordinary,
                    dps_forecast_annual=dps,
                    forecast_period_start=target[0] if target else None,
                    forecast_period_end=target[1] if target else None,
                    forecast_basis=source_basis,
                    forecast_target_basis=target_basis,
                    forecast_field_sources=tuple(field_sources),
                    forecast_status=earnings_status,
                    dividend_forecast_status=dividend_status,
                )
            )
    return sorted(projected, key=disclosure_revision)


def latest_forecast_source(
    rows: Sequence[JQuantsFinancialSummary], *, dividend: bool = False
) -> JQuantsFinancialSummary | None:
    """Resolve revision ties by value agreement, never by DocNo or input order."""
    status_field = "dividend_forecast_status" if dividend else "forecast_status"
    candidates = [row for row in rows if getattr(row, status_field) in {"confirmed", "unresolved"}]
    if not candidates:
        candidates = [row for row in rows if getattr(row, status_field) == "missing"]
    if not candidates:
        return None
    latest = latest_disclosure_candidates(candidates)
    fields = (
        ("dps_forecast_annual",)
        if dividend
        else ("forecast_eps", "forecast_profit", "forecast_ordinary_profit")
    )
    source = max(
        latest,
        key=lambda row: (
            sum(getattr(row, field) is not None for field in fields),
            tuple(getattr(row, field) is not None for field in fields),
        ),
    )
    if len(latest) > 1:
        kinds = {row.document_type for row in latest}
        source = replace(
            source,
            disclosure_number=None,
            document_type=next(iter(kinds)) if len(kinds) == 1 else None,
            disclosed_time=source.disclosed_time
            if all(row.disclosed_time == source.disclosed_time for row in latest)
            else None,
            forecast_source_documents=tuple(
                sorted({row.disclosure_number for row in latest if row.disclosure_number})
            ),
        )
    agreed: dict[str, float | None] = {}
    conflicts = set()
    field_sources = []
    for field in fields:
        observed = [row for row in latest if getattr(row, field) is not None]
        values = {getattr(row, field) for row in observed}
        unknown = [
            row
            for row in latest
            if getattr(row, field) is None
            and any(name == field and records for name, records in row.forecast_field_sources)
        ]
        agreed[field] = next(iter(values)) if len(values) == 1 and not unknown else None
        if len(values) > 1 or unknown:
            conflicts.add(field)
        records = {
            key
            for row in latest
            for name, keys in row.forecast_field_sources
            if name == field
            for key in keys
        }
        records.update(row.disclosure_number for row in observed if row.disclosure_number)
        field_sources.append((field, tuple(sorted(records))))
    if len(latest) > 1:
        source = replace(
            source,
            document_payload=None,
            forecast_field_sources=tuple(field_sources),
        )
    if dividend:
        source = replace(source, dps_forecast_annual=agreed["dps_forecast_annual"])
        if conflicts:
            return replace(source, dividend_forecast_status="unresolved")
    else:
        source = replace(
            source,
            forecast_eps=agreed["forecast_eps"],
            forecast_profit=agreed["forecast_profit"],
            forecast_ordinary_profit=agreed["forecast_ordinary_profit"],
        )
        if conflicts:
            return replace(
                source,
                forecast_status="partially_unresolved"
                if any(value is not None for value in agreed.values())
                else "unresolved",
            )
    if getattr(source, status_field) == "confirmed" and all(
        getattr(source, field) is None for field in fields
    ):
        return (
            replace(source, dividend_forecast_status="unresolved")
            if dividend
            else replace(source, forecast_status="unresolved")
        )
    return source


def forecast_source_trace(
    row: JQuantsFinancialSummary | None, *, dividend: bool = False
) -> dict[str, str | None] | None:
    if row is None:
        return None
    trace = {
        "status": row.dividend_forecast_status if dividend else row.forecast_status,
        "disclosure_number": row.disclosure_number,
        "disclosed_at": row.disclosed_at.isoformat(),
        "disclosed_time": row.disclosed_time,
        "document_type": row.document_type,
        "period_start": row.forecast_period_start.isoformat()
        if row.forecast_period_start
        else None,
        "period_end": row.forecast_period_end.isoformat() if row.forecast_period_end else None,
        "basis": "issuer_dividend" if dividend else row.forecast_basis,
        "target_basis": "issuer_dividend" if dividend else row.forecast_target_basis,
    }
    if row.forecast_source_documents:
        trace["candidate_disclosure_numbers"] = ",".join(row.forecast_source_documents)
    for field, records in row.forecast_field_sources:
        if (field == "dps_forecast_annual") != dividend:
            continue
        trace[f"{field}_source_records"] = ",".join(records) or None
    return trace
