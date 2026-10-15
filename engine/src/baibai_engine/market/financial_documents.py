"""Financial disclosure decoding and point-in-time forecast projection; no I/O."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, time
from math import isfinite
from typing import Any

from baibai_engine.market.jquants_decode import (
    coalesce_field,
    first_period_float,
    first_value,
    has_period_slot,
    parse_date,
    parse_jquants_code_parts,
    parse_optional_date,
    to_float,
    to_period,
)
from baibai_engine.market.jquants_models import JQuantsFinancialSummary


def normalize_financial_summary(record: Mapping[str, Any]) -> JQuantsFinancialSummary | None:
    ticker, common_code = parse_jquants_code_parts(first_value(record, "Code", "code"))
    if not common_code:
        return None
    # 期間metadataのないlegacy入力だけ、EPSと同じ当期/翌期slotから利益予想を採る。
    # metadataのある文書は両slotをpayloadへ残し、as-ofの投影で対象期とbasisを解決する。
    legacy_forecast = _period(record, "current") is None
    if not legacy_forecast:
        forecast_profit = forecast_ordinary_profit = None
    elif has_period_slot(record, "FEPS"):
        forecast_profit = to_float(coalesce_field(record, "FNP"))
        forecast_ordinary_profit = to_float(coalesce_field(record, "FOdP"))
    else:
        forecast_profit = to_float(coalesce_field(record, "NxFNp"))
        forecast_ordinary_profit = to_float(coalesce_field(record, "NxFOdP"))
    # すべて `coalesce_field` 経由にして 0.0 の数値フィールドを欠損と誤判定しないようにする
    # (EPS=0 の赤字転換点、Sales=0 の新規事業初期、OP=0 の損益分岐点ちょうど、など)。
    return JQuantsFinancialSummary(
        ticker=ticker,
        disclosed_at=parse_date(
            first_value(
                record, "DisclosedDate", "disclosed_at", "DiscDate", "disc_date", "Date", "date"
            )
        ),
        # legacyの当期→翌期fallback。期間が分かる文書の予想はここで選ばない。
        forecast_eps=first_period_float(record, "FEPS", "NxFEPS") if legacy_forecast else None,
        eps_ttm=to_float(
            coalesce_field(
                record,
                "EpsTtm",
                "eps_ttm",
                "EarningsPerShare",
                "earnings_per_share",
                "EPS",
                "eps",
            )
        ),
        bps=to_float(
            coalesce_field(record, "BPS", "bps", "BookValuePerShare", "book_value_per_share")
        ),
        shares_outstanding=to_float(
            coalesce_field(
                record,
                "SharesOutstanding",
                "shares_outstanding",
                "IssuedShareEquityQuote",
                "issued_share_equity_quote",
                "ShOutFY",
            )
        ),
        sales=to_float(coalesce_field(record, "NetSales", "net_sales", "Sales", "sales")),
        cfo=to_float(
            coalesce_field(
                record,
                "CashFlowsFromOperatingActivities",
                "cash_flows_from_operating_activities",
                "OperatingCashFlow",
                "operating_cash_flow",
                "CFO",
                "cfo",
            )
        ),
        cash_eq=to_float(
            coalesce_field(
                record,
                "CashAndEquivalents",
                "cash_and_equivalents",
                "CashEq",
                "cash_eq",
            )
        ),
        total_assets=to_float(coalesce_field(record, "TotalAssets", "total_assets", "TA", "ta")),
        equity=to_float(coalesce_field(record, "Equity", "equity", "Eq", "eq")),
        operating_profit=to_float(
            coalesce_field(record, "OperatingProfit", "operating_profit", "OP")
        ),
        ordinary_profit=to_float(
            coalesce_field(record, "OrdinaryProfit", "ordinary_profit", "OdP")
        ),
        profit=to_float(coalesce_field(record, "Profit", "profit", "NP")),
        forecast_profit=forecast_profit,
        forecast_ordinary_profit=forecast_ordinary_profit,
        fiscal_period=to_period(
            coalesce_field(
                record,
                "TypeOfCurrentPeriod",
                "type_of_current_period",
                "CurPerType",
            )
        ),
        fiscal_year_end=parse_optional_date(
            coalesce_field(
                record,
                "CurrentFiscalYearEndDate",
                "current_fiscal_year_end_date",
                "CurFYEn",
            )
        ),
        period_start=parse_optional_date(
            coalesce_field(
                record,
                "CurrentPeriodStartDate",
                "current_period_start_date",
                "CurPerSt",
            )
        ),
        period_end=parse_optional_date(
            coalesce_field(
                record,
                "CurrentPeriodEndDate",
                "current_period_end_date",
                "CurPerEn",
            )
        ),
        # 実績年間DPSと、legacy限定の予想fallback。文書予想は対象期の投影へ委ねる。
        dps_actual_annual=to_float(coalesce_field(record, "DivAnn")),
        dps_forecast_annual=first_period_float(record, "FDivAnn", "NxFDivAnn")
        if legacy_forecast
        else None,
        # TrShFY = 期末自己株式数、EqAR = 開示された自己資本比率。ShOutFY (発行済・自己株
        # 込み) と Eq (純資産) だけでは時価総額も自己資本比率も正しい分母で作れない。
        treasury_shares=to_float(
            coalesce_field(record, "TrShFY", "treasury_shares", "TreasuryStock")
        ),
        equity_to_asset_ratio=to_float(
            coalesce_field(record, "EqAR", "equity_to_asset_ratio", "EquityToAssetRatio")
        ),
        # 支払ごとの 1 株当たり配当。各支払の基準日 (四半期末) より後の調整だけを掛ければ
        # as-of 基準へ寄せられる。DivTotalAnn は円なので株式基準を持たず、自己株式を除いた
        # 株式数で割った値がその換算の独立した照合になる。AvgSh はその株数の健全性を同じ
        # 行の中で確かめるためのアンカー。
        dividend_q1=to_float(coalesce_field(record, "Div1Q")),
        dividend_interim=to_float(coalesce_field(record, "Div2Q")),
        dividend_q3=to_float(coalesce_field(record, "Div3Q")),
        dividend_year_end=to_float(coalesce_field(record, "DivFY")),
        dividend_total_annual=to_float(coalesce_field(record, "DivTotalAnn")),
        average_shares=to_float(coalesce_field(record, "AvgSh")),
        record_key=financial_document_key(record),
        disclosure_number=_text(record.get("DiscNo")),
        disclosed_time=_disclosed_time(record.get("DiscTime")),
        document_type=_text(record.get("DocType")),
        document_payload=financial_document_payload(record),
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


def financial_document_payload(record: Mapping[str, Any]) -> str:
    """Retain every provider field, including empty slots and both bases.

    Non-finite SDK values are explicit unknown strings, not JSON NaN or zero.
    """
    values = {
        str(key): str(value) if isinstance(value, float) and not isfinite(value) else value
        for key, value in record.items()
    }
    return json.dumps(values, ensure_ascii=False, sort_keys=True, allow_nan=False, default=str)


def financial_document_key(record: Mapping[str, Any]) -> str:
    number = _text(record.get("DiscNo"))
    if number is not None:
        return f"doc:{number}"
    return "payload:" + hashlib.sha256(financial_document_payload(record).encode()).hexdigest()


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


def project_financial_documents(
    summaries: Sequence[JQuantsFinancialSummary], asof_date: date
) -> list[JQuantsFinancialSummary]:
    """Project each issuer's applicable period without changing actual facts.

    Choose fiscal-period state before looking for numeric guidance. An empty new
    period cannot revive an older period's forecast. Earnings and DPS are independent
    document streams; the three earnings fields stay in one slot and one basis.
    Rows without source period metadata retain the legacy mechanical inputs, explicitly
    unconfirmed. They cannot override confirmed document guidance.
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
                    eps, profit, ordinary = (to_float(record.get(key)) for key in keys)
                    for field, key in zip(
                        ("forecast_eps", "forecast_profit", "forecast_ordinary_profit"),
                        keys,
                        strict=True,
                    ):
                        if has_period_slot(record, key) and row.record_key:
                            field_sources.append((field, (row.record_key,)))
                dps_key = f"{prefix}DivAnn"
                dividend_status = "confirmed" if has_period_slot(record, dps_key) else "missing"
                dps = to_float(record.get(dps_key))
                if has_period_slot(record, dps_key) and row.record_key:
                    field_sources.append(("dps_forecast_annual", (row.record_key,)))
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
    known = any(getattr(row, status_field) != "legacy_unknown" for row in rows)
    candidates = (
        [row for row in rows if getattr(row, status_field) == "confirmed"] if known else list(rows)
    )
    if not candidates and known:
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
        records.update(row.record_key for row in observed if row.record_key)
        field_sources.append((field, tuple(sorted(records))))
    if len(latest) > 1:
        source = replace(
            source,
            record_key=None,
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
