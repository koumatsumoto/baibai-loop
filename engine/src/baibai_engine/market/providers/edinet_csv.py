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

from baibai_engine.market.edinet_decode import to_float
from baibai_engine.market.edinet_models import EdinetMetricRecord, EDINETProviderError
from baibai_engine.market.metric_quality import TTMQuality


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
    investment_securities = _single_metric(rows, _TAGS["investment_securities"], basis=basis)
    equity = _single_metric(rows, _TAGS["equity"], basis=basis)
    total_assets = _single_metric(rows, _TAGS["total_assets"], basis=basis)
    debt = _debt_metric(rows, basis=basis)
    depreciation = _sum_metric(rows, _TAGS["depreciation"], basis=basis)
    capex = _capex_metric(rows, basis=basis)
    capex_abs = abs(capex) if capex is not None else None
    details = _cash_flow_details(rows, basis=basis) if doc_type_code in {"120", "130"} else {}
    ebitda = (
        operating_profit + depreciation
        if operating_profit is not None and depreciation is not None
        else None
    )
    fcf = ocf - capex_abs if ocf is not None and capex_abs is not None else None
    net_cash = cash - debt if cash is not None and debt is not None else None

    for name, value in (
        ("sales", sales),
        ("ocf", ocf),
        ("cash", cash),
        ("investment_securities", investment_securities),
        ("capex", capex_abs),
    ):
        if value is None:
            failures.append(f"metric_unavailable:{name}")
    if debt is None:
        failures.append("metric_unavailable:debt")

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
        ocf_receivables_cash_effect=details.get("ocf_receivables_cash_effect"),
        ocf_inventories_cash_effect=details.get("ocf_inventories_cash_effect"),
        ocf_payables_cash_effect=details.get("ocf_payables_cash_effect"),
        ocf_contract_liabilities_cash_effect=details.get("ocf_contract_liabilities_cash_effect"),
        ocf_advances_received_cash_effect=details.get("ocf_advances_received_cash_effect"),
        ocf_other_payables_cash_effect=details.get("ocf_other_payables_cash_effect"),
        capex_ppe_reported=details.get("capex_ppe_reported"),
        capex_intangible_reported=details.get("capex_intangible_reported"),
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
        "convertiblebondtypebondswithsubscriptionrightstoshares",
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
    "ocf_receivables_cash_effect": ("decreaseincreaseinnotesandaccountsreceivabletradeopecf",),
    "ocf_inventories_cash_effect": ("decreaseincreaseininventoriesopecf",),
    "ocf_payables_cash_effect": ("increasedecreaseinnotesandaccountspayabletradeopecf",),
    "ocf_contract_liabilities_cash_effect": ("increasedecreaseincontractliabilitiesopecf",),
    "ocf_advances_received_cash_effect": ("increasedecreaseinadvancesreceivedopecf",),
    "ocf_other_payables_cash_effect": ("increasedecreaseinaccountspayableotheropecf",),
}


def _read_csv_zip_rows(content: bytes) -> list[dict[str, str]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise EDINETProviderError("zip_invalid") from exc
    with archive:
        rows: list[dict[str, str]] = []
        for name in archive.namelist():
            if not name.lower().endswith(".csv"):
                continue
            with archive.open(name) as handle:
                raw = handle.read()
            text = _decode_csv(raw)
            reader = csv.DictReader(io.StringIO(text), delimiter="\t")
            headers = ["".join(name.split()) for name in (reader.fieldnames or [])]
            required = {"要素ID", "コンテキストID", "ユニットID", "値"}
            if len(headers) != len(set(headers)) or not required.issubset(headers):
                raise EDINETProviderError("csv_parse_failed: missing or duplicate native headers")
            reader.fieldnames = headers
            rows.extend(dict(row) for row in reader if row)
    return rows


def _decode_csv(content: bytes) -> str:
    try:
        return content.decode("utf-16-le").lstrip("\ufeff")
    except UnicodeDecodeError as exc:
        raise EDINETProviderError("csv_parse_failed") from exc


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
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


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


def _cash_flow_details(rows: Sequence[Mapping[str, str]], *, basis: str) -> dict[str, float | None]:
    periods = _ranked_metric_values(rows, _TAGS["ocf"], basis=basis)
    if not periods:
        return {}
    context = max(periods, key=lambda item: item[0])[0][2]
    fields = (
        "ocf_receivables_cash_effect",
        "ocf_inventories_cash_effect",
        "ocf_payables_cash_effect",
        "ocf_contract_liabilities_cash_effect",
        "ocf_advances_received_cash_effect",
        "ocf_other_payables_cash_effect",
    )
    details = {
        field: _unique_jpy_metric(rows, _TAGS[field], basis=basis, context=context)
        for field in fields
    }
    ppe = _unique_jpy_metric(rows, _TAGS["capex_tangible"], basis=basis, context=context)
    intangible = _unique_jpy_metric(rows, _TAGS["capex_intangible"], basis=basis, context=context)
    details["capex_ppe_reported"] = abs(ppe) if ppe is not None else None
    details["capex_intangible_reported"] = abs(intangible) if intangible is not None else None
    return details


def _unique_jpy_metric(
    rows: Sequence[Mapping[str, str]],
    tags: tuple[str, ...],
    *,
    basis: str,
    context: str,
) -> float | None:
    values: set[float] = set()
    for row in rows:
        if _row_context(row) != context or not _element_matches(_row_element(row), tags):
            continue
        if basis == "consolidated" and not _is_consolidated_row(row):
            continue
        if basis == "non_consolidated" and _is_consolidated_row(row):
            continue
        if _row_unit(row) != "JPY":
            continue
        value = _csv_value(_row_value(row))
        if value is None:
            return None
        values.add(value)
    return next(iter(values)) if len(values) == 1 else None


def _debt_metric(rows: Sequence[Mapping[str, str]], *, basis: str) -> float | None:
    total = _single_metric(rows, _TAGS["debt_total"], basis=basis)
    return total if total is not None else _sum_metric(rows, _TAGS["debt_components"], basis=basis)


def _best_metric_values_by_element(
    rows: Sequence[Mapping[str, str]],
    tags: tuple[str, ...],
    *,
    basis: str,
) -> list[float | None]:
    best_by_element: dict[str, tuple[tuple[int, int, str], float | None]] = {}
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
) -> list[tuple[tuple[int, int, str], float | None, str]]:
    grouped: dict[tuple[str, str, str], set[float | None]] = {}
    for row in rows:
        raw_element = _row_element(row)
        if not _element_matches(raw_element, tags):
            continue
        if basis == "consolidated" and not _is_consolidated_row(row):
            continue
        if basis == "non_consolidated" and _is_consolidated_row(row):
            continue
        key = (_normalize_element(raw_element), _row_context(row), _row_basis(row))
        values = grouped.setdefault(key, set())
        if _row_unit(row) == "JPY":
            values.add(_csv_value(_row_value(row)))
    # Keep an unusable current fact in the ranking: currency/missingness must not
    # make an older period win. Conflicting duplicate JPY facts have no chosen value.
    return [
        (_context_rank(context), next(iter(values)) if len(values) == 1 else None, element)
        for (element, context, _), values in grouped.items()
    ]


def _row_element(row: Mapping[str, str]) -> str:
    return row.get("要素ID", "")


def _row_context(row: Mapping[str, str]) -> str:
    return row.get("コンテキストID", "")


def _is_consolidated_row(row: Mapping[str, str]) -> bool:
    basis = _row_basis(row).strip()
    if basis in {"個別", "非連結", "non_consolidated"}:
        return False
    return basis in {"連結", "consolidated"} or _is_consolidated_context(_row_context(row))


def _is_consolidated_context(context: str) -> bool:
    lowered = context.lower()
    return "consolidated" in lowered and "nonconsolidated" not in lowered


def _row_basis(row: Mapping[str, str]) -> str:
    return row.get("連結・個別", "")


def _row_value(row: Mapping[str, str]) -> str | None:
    return row.get("値")


def _row_unit(row: Mapping[str, str]) -> str:
    return row.get("ユニットID", "")


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


def _csv_value(raw: str | None) -> float | None:
    # EDINET CSV defines only the ASCII hyphen as explicit zero.
    return 0.0 if raw is not None and raw.strip() == "-" else to_float(raw)
