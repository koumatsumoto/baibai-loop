"""EDINET CSV-ZIP financial statement parsing into metric records.

Extracts TTM metrics from the CSV bundle attached to EDINET filings. Kept
separate from the API provider: this is pure parsing over downloaded bytes.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Mapping, Sequence
from datetime import date

from baibai_engine.screening.metric_quality import TTMQuality

from .edinet import EdinetMetricRecord, EDINETProviderError, _coalesce, _to_float


def parse_csv_zip_metric_record(
    *,
    ticker: str,
    doc_id: str,
    doc_type_code: str,
    content: bytes,
    submit_datetime: str | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
) -> EdinetMetricRecord:
    rows = _read_csv_zip_rows(content)
    if not rows:
        return EdinetMetricRecord(
            ticker=ticker,
            source_doc_id=doc_id,
            document_type=doc_type_code,
            source_submit_datetime=submit_datetime,
            source_period_start=period_start,
            source_period_end=period_end,
            failure_reasons=("csv_parse_failed",),
        )

    quality = TTMQuality.EXACT if doc_type_code in {"120", "130"} else TTMQuality.APPROXIMATED
    basis = _detect_consolidation_basis(rows)
    failures: list[str] = []
    if basis == "non_consolidated":
        failures.append("non_consolidated_fallback")

    sales = _single_metric(rows, _TAGS["sales"], basis=basis)
    ocf = _single_metric(rows, _TAGS["ocf"], basis=basis)
    operating_profit = _single_metric(rows, _TAGS["operating_profit"], basis=basis)
    cash = _single_metric(rows, _TAGS["cash"], basis=basis)
    investment_securities = _balance_sheet_metric(rows, _TAGS["investment_securities"], basis=basis)
    equity = _single_metric(rows, _TAGS["equity"], basis=basis)
    total_assets = _single_metric(rows, _TAGS["total_assets"], basis=basis)
    debt = _debt_metric(rows, basis=basis)
    depreciation = _sum_metric(rows, _TAGS["depreciation"], basis=basis)
    capex = _capex_metric(rows, basis=basis)
    capex_abs = abs(capex) if capex is not None else None
    ebitda = (
        operating_profit + depreciation
        if operating_profit is not None and depreciation is not None
        else None
    )
    fcf = ocf - capex_abs if ocf is not None and capex_abs is not None else None
    debt_assumed_zero = debt is None and cash is not None
    net_cash = cash - debt if cash is not None and debt is not None else None

    for name, value in (
        ("sales", sales),
        ("ocf", ocf),
        ("cash", cash),
        ("investment_securities", investment_securities),
        ("capex", capex_abs),
    ):
        if value is None:
            failures.append(f"tag_not_found:{name}")
    if debt_assumed_zero:
        failures.append("debt_assumed_zero")
    elif debt is None:
        failures.append("tag_not_found:debt")

    return EdinetMetricRecord(
        ticker=ticker,
        sales_ttm=sales,
        ocf_ttm=ocf,
        debt=debt,
        cash=cash,
        investment_securities=investment_securities,
        ebitda_ttm=ebitda,
        consolidation_basis=basis,
        ttm_quality_ev_ebitda=quality if ebitda is not None else TTMQuality.UNAVAILABLE,
        ttm_quality_p_s=quality if sales is not None else TTMQuality.UNAVAILABLE,
        ttm_quality_pcfr=quality if ocf is not None else TTMQuality.UNAVAILABLE,
        operating_profit_ttm=operating_profit,
        depreciation_and_amortization_ttm=depreciation,
        capex_ttm=capex_abs,
        fcf_ttm=fcf,
        net_cash=net_cash,
        equity=equity,
        total_assets=total_assets,
        ttm_quality_fcf=quality if fcf is not None else TTMQuality.UNAVAILABLE,
        ttm_quality_net_cash=quality if net_cash is not None else TTMQuality.UNAVAILABLE,
        source_doc_id=doc_id,
        document_type=doc_type_code,
        source_submit_datetime=submit_datetime,
        source_period_start=period_start,
        source_period_end=period_end,
        capex_source="purchase_of_fixed_assets" if capex_abs is not None else None,
        failure_reasons=tuple(dict.fromkeys(failures)),
    )


_TAGS: dict[str, tuple[str, ...]] = {
    "sales": ("netsales", "revenuesfromexternalcustomers"),
    "ocf": ("cashflowsfromoperatingactivities", "netcashprovidedbyusedinoperatingactivities"),
    "operating_profit": ("operatingprofit", "operatingincome"),
    "cash": ("cashanddeposits", "cashandcashequivalents"),
    "investment_securities": ("investmentsecurities",),
    "equity": ("equity", "totalequity", "netassets"),
    "total_assets": ("totalassets", "assets"),
    "debt_total": (
        "interestbearingdebt",
        "interestbearingliabilities",
    ),
    "debt_components": (
        "shorttermborrowings",
        "shorttermloanspayable",
        "currentportionoflongtermborrowings",
        "currentportionoflongtermloanspayable",
        "currentportionofbonds",
        "currentportionofbondspayable",
        "bondspayable",
        "longtermborrowings",
        "longtermloanspayable",
        "leaseobligations",
        "leaseobligationscl",
        "leaseobligationsncl",
    ),
    "depreciation": (
        "depreciationandamortization",
        "depreciationandamortizationopecf",
        "depreciation",
        "amortizationofgoodwill",
        "amortizationofgoodwillopecf",
    ),
    "capex_total": ("purchaseofpropertyplantandequipmentandintangibleassetsinvcf",),
    "capex_tangible": (
        "purchaseofpropertyplantandequipment",
        "purchaseofpropertyplantandequipmentinvcf",
        "paymentsforpurchaseofpropertyplantandequipment",
    ),
    "capex_intangible": (
        "purchaseofintangibleassets",
        "purchaseofintangibleassetsinvcf",
        "paymentsforpurchaseofintangibleassets",
    ),
}


def _read_csv_zip_rows(content: bytes) -> list[dict[str, str]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise EDINETProviderError("zip_invalid") from exc
    rows: list[dict[str, str]] = []
    for name in archive.namelist():
        if not name.lower().endswith(".csv"):
            continue
        with archive.open(name) as handle:
            raw = handle.read()
        text = _decode_csv(raw)
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        rows.extend(dict(row) for row in reader if row)
    return rows


def _decode_csv(content: bytes) -> str:
    for encoding in ("utf-16", "utf-16-le", "utf-8-sig", "cp932"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise EDINETProviderError("csv_parse_failed")


def _detect_consolidation_basis(rows: Sequence[Mapping[str, str]]) -> str:
    if any(_is_consolidated_row(row) for row in rows):
        return "consolidated"
    return "non_consolidated"


def _single_metric(
    rows: Sequence[Mapping[str, str]],
    tags: tuple[str, ...],
    *,
    basis: str,
) -> float | None:
    values = _ranked_metric_values(rows, tags, basis=basis)
    return max(values, key=lambda item: item[0])[1] if values else None


def _sum_metric(
    rows: Sequence[Mapping[str, str]],
    tags: tuple[str, ...],
    *,
    basis: str,
) -> float | None:
    values = _best_metric_values_by_element(rows, tags, basis=basis)
    return sum(values) if values else None


def _capex_metric(rows: Sequence[Mapping[str, str]], *, basis: str) -> float | None:
    # CFOと同じ期間・範囲を使う。合算の欠如を前期の合算で補わず、
    # 個別内訳も同じcontextの有形・無形が揃う場合だけ足す。
    periods = _ranked_metric_values(rows, _TAGS["ocf"], basis=basis)
    if not periods:
        periods = _ranked_metric_values(
            rows,
            (*_TAGS["capex_total"], *_TAGS["capex_tangible"], *_TAGS["capex_intangible"]),
            basis=basis,
        )
    if not periods:
        return None
    context = max(periods, key=lambda item: item[0])[0][2]
    current = [row for row in rows if _row_context(row) == context]
    total = _single_metric(current, _TAGS["capex_total"], basis=basis)
    if total is not None:
        return total
    tangible = _single_metric(current, _TAGS["capex_tangible"], basis=basis)
    intangible = _single_metric(current, _TAGS["capex_intangible"], basis=basis)
    return tangible + intangible if tangible is not None and intangible is not None else None


def _debt_metric(rows: Sequence[Mapping[str, str]], *, basis: str) -> float | None:
    total = _single_metric(rows, _TAGS["debt_total"], basis=basis)
    if total is not None:
        return total
    components = _sum_metric(rows, _TAGS["debt_components"], basis=basis)
    if components is not None:
        return components
    if _has_zero_like_metric(rows, (*_TAGS["debt_total"], *_TAGS["debt_components"]), basis=basis):
        return 0.0
    return None


def _balance_sheet_metric(
    rows: Sequence[Mapping[str, str]],
    tags: tuple[str, ...],
    *,
    basis: str,
) -> float | None:
    value = _single_metric(rows, tags, basis=basis)
    if value is not None:
        return value
    if _has_zero_like_metric(rows, tags, basis=basis):
        return 0.0
    return None


def _best_metric_values_by_element(
    rows: Sequence[Mapping[str, str]],
    tags: tuple[str, ...],
    *,
    basis: str,
) -> list[float]:
    best_by_element: dict[str, tuple[tuple[int, int, str], float]] = {}
    for rank, value, element in _ranked_metric_values(rows, tags, basis=basis):
        current = best_by_element.get(element)
        if current is None or rank > current[0]:
            best_by_element[element] = (rank, value)
    return [value for _, value in best_by_element.values()]


def _ranked_metric_values(
    rows: Sequence[Mapping[str, str]],
    tags: tuple[str, ...],
    *,
    basis: str,
) -> list[tuple[tuple[int, int, str], float, str]]:
    values: list[tuple[tuple[int, int, str], float, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        raw_element = _row_element(row)
        element = _normalize_element(raw_element)
        if not _element_matches(raw_element, tags):
            continue
        context = _row_context(row)
        if basis == "consolidated" and not _is_consolidated_row(row):
            continue
        if basis == "non_consolidated" and _is_consolidated_row(row):
            continue
        value = _to_float(_row_value(row))
        if value is None:
            continue
        key = (element, context, _row_basis(row))
        if key in seen:
            continue
        seen.add(key)
        values.append((_context_rank(context), value, element))
    return values


def _has_zero_like_metric(
    rows: Sequence[Mapping[str, str]],
    tags: tuple[str, ...],
    *,
    basis: str,
) -> bool:
    found = False
    for row in rows:
        raw_element = _row_element(row)
        if not _element_matches(raw_element, tags):
            continue
        if basis == "consolidated" and not _is_consolidated_row(row):
            continue
        if basis == "non_consolidated" and _is_consolidated_row(row):
            continue
        found = True
        if not _is_zero_like(_row_value(row)):
            return False
    return found


def _row_element(row: Mapping[str, str]) -> str:
    return str(_coalesce(row, "要素ID", "element_id", "ElementID", "elementId") or "")


def _row_context(row: Mapping[str, str]) -> str:
    return str(_coalesce(row, "コンテキストID", "context_id", "ContextID", "contextId") or "")


def _is_consolidated_row(row: Mapping[str, str]) -> bool:
    return "連結" in _row_basis(row) or _is_consolidated_context(_row_context(row))


def _is_consolidated_context(context: str) -> bool:
    lowered = context.lower()
    return "consolidated" in lowered and "nonconsolidated" not in lowered


def _row_basis(row: Mapping[str, str]) -> str:
    return str(_coalesce(row, "連結・個別", "consolidation_basis", "ConsolidationBasis") or "")


def _row_value(row: Mapping[str, str]) -> str | None:
    value = _coalesce(row, "値", "value", "Value", "金額")
    return str(value) if value is not None else None


def _normalize_element(value: str) -> str:
    return "".join(char for char in value.lower() if char.isalnum())


def _element_matches(element: str, tags: tuple[str, ...]) -> bool:
    local_name = _normalize_element(element.rsplit(":", 1)[-1])
    return local_name in tags


def _context_rank(context: str) -> tuple[int, int, str]:
    lowered = context.lower()
    current = 0 if "prior" in lowered else 1
    primary = 0 if "_" in context or "member" in lowered else 1
    return (current, primary, context)


def _is_zero_like(value: str | None) -> bool:
    return str(value or "").strip().replace(",", "") in {"0", "0.0", "-", "－", "―", "–"}
