"""Financial disclosure decoding and point-in-time forecast projection; no I/O."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
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
    # 会社予想の純利益/経常のペアは forecast_eps と同一予想期から採る。当期予想 EPS
    # (FEPS) が埋まっていれば当期予想の FNP/FOdP、本決算開示で FEPS が空なら翌期
    # ガイダンスの NxFNp/NxFOdP を採る (forecast_eps の FEPS→NxFEPS と同じ期選択)。
    # 期をまたいだ比較 (当期 EPS 期 と翌期利益の突合) は一時益 flag の誤検出になるので混ぜない。
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
        # ClientV2 の fin-summary は短縮キーを返す。FEPS=当期予想 EPS は本決算(FY)
        # 開示で空になり、翌期ガイダンスは NxFEPS に入る。FEPS→NxFEPS の順で各時点の
        # 最良 forward EPS(per_forward の基)を埋める。
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
        # DivAnn=実績年間 DPS。予想年間は FDivAnn (四半期) → NxFDivAnn (本決算の
        # 進行期ガイダンス) の順で埋める (FEPS→NxFEPS と同型)。
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
        disclosed_time=_text(record.get("DiscTime")),
        document_type=_text(record.get("DocType")),
        document_payload=financial_document_payload(record),
    )


def _text(value: Any) -> str | None:
    return str(value).strip() if value is not None and str(value).strip() else None


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
    """Document numbers identify documents; they do not establish chronology."""
    return row.disclosed_at, row.disclosed_time or ""


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
        states: list[tuple[date, tuple[date, str], tuple[date, date]]] = []
        for i in known:
            for slot in ("current", "next"):
                period = _period(records[i], slot)
                if period is not None and period[0] <= asof_date:
                    states.append((period[0], disclosure_revision(rows[i]), period))
        state_key = max(((s[0], s[1]) for s in states), default=None)
        targets = {s[2] for s in states if (s[0], s[1]) == state_key}
        target = next(iter(targets)) if len(targets) == 1 else None
        if target is not None and target[1] < asof_date:
            target = None
        statements = [row for row in rows if "FinancialStatements" in (row.document_type or "")]
        statement = max(
            statements,
            key=lambda r: (r.fiscal_year_end or date.min, disclosure_revision(r)),
            default=None,
        )
        basis = (
            "nonconsolidated"
            if statement is not None and "NonConsolidated" in (statement.document_type or "")
            else "consolidated"
            if statement is not None
            else "primary_unknown"
        )
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
                keys = (
                    nc_keys
                    if basis == "nonconsolidated"
                    and any(has_period_slot(record, key) for key in nc_keys)
                    else primary_keys
                )
                earnings_status = (
                    "confirmed" if any(has_period_slot(record, key) for key in keys) else "missing"
                )
                eps, profit, ordinary = (to_float(record.get(key)) for key in keys)
                dps_key = f"{prefix}DivAnn"
                dividend_status = "confirmed" if has_period_slot(record, dps_key) else "missing"
                dps = to_float(record.get(dps_key))
            projected.append(
                replace(
                    row,
                    forecast_eps=eps,
                    forecast_profit=profit,
                    forecast_ordinary_profit=ordinary,
                    dps_forecast_annual=dps,
                    forecast_period_start=target[0] if target else None,
                    forecast_period_end=target[1] if target else None,
                    forecast_basis=basis,
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
    revision = max(map(disclosure_revision, candidates))
    latest = [row for row in candidates if disclosure_revision(row) == revision]
    fields = (
        ("dps_forecast_annual",)
        if dividend
        else ("forecast_eps", "forecast_profit", "forecast_ordinary_profit")
    )
    values = {tuple(getattr(row, field) for field in fields) for row in latest}
    source = latest[0]
    if len(latest) > 1:
        source = replace(
            source,
            disclosure_number=None,
            forecast_source_documents=tuple(
                sorted({row.disclosure_number for row in latest if row.disclosure_number})
            ),
        )
    if len(values) != 1:
        return (
            replace(source, dps_forecast_annual=None, dividend_forecast_status="unresolved")
            if dividend
            else replace(
                source,
                forecast_eps=None,
                forecast_profit=None,
                forecast_ordinary_profit=None,
                forecast_status="unresolved",
            )
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
    }
    if row.forecast_source_documents:
        trace["candidate_disclosure_numbers"] = ",".join(row.forecast_source_documents)
    return trace
