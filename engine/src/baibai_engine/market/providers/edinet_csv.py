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
    rows, basis = _select_statement_rows(rows, doc_type_code)
    failures: list[str] = []
    if basis == "non_consolidated":
        failures.append("non_consolidated_fallback")

    sales = _single_metric(rows, _TAGS["sales"])
    ocf = _single_metric(rows, _TAGS["ocf"])
    operating_profit = _single_metric(rows, _TAGS["operating_profit"])
    cash = _single_metric(rows, _TAGS["cash"])
    investment_securities = _single_metric(rows, _TAGS["investment_securities"])
    equity = _single_metric(rows, _TAGS["equity"])
    total_assets = _single_metric(rows, _TAGS["total_assets"])
    debt = _debt_metric(rows)
    depreciation = _sum_metric(rows, _TAGS["depreciation"])
    capex = _capex_metric(rows)
    capex_abs = abs(capex) if capex is not None else None
    details = _cash_flow_details(rows) if doc_type_code in {"120", "130"} else {}
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


def _select_statement_rows(
    rows: Sequence[Mapping[str, str]], doc_type_code: str
) -> tuple[list[Mapping[str, str]], str | None]:
    # Financial-statement contexts, not arbitrary current-looking IDs. Quarterly
    # flow values use YTD, not the standalone quarter. Extra dimensions are not
    # company totals; prior periods never fill missing current facts.
    if doc_type_code in {"120", "130"}:
        contexts = {"CurrentYearInstant", "CurrentYearDuration"}
    elif doc_type_code in {"160", "170"}:
        contexts = {"InterimInstant", "InterimDuration"}
    elif doc_type_code in {"140", "150"}:
        contexts = {"CurrentQuarterInstant", "CurrentYTDDuration"}
    else:
        raise EDINETProviderError(f"unsupported financial statement type: {doc_type_code}")
    tags = tuple(tag for group in _TAGS.values() for tag in group)
    financial = [row for row in rows if _element_matches(_row_element(row), tags)]
    consolidated = [row for row in financial if _row_context(row) in contexts]
    if consolidated:
        return consolidated, "consolidated"
    individual_contexts = {f"{context}_NonConsolidatedMember" for context in contexts}
    individual = [row for row in financial if _row_context(row) in individual_contexts]
    return individual, "non_consolidated" if individual else None


def _metric_values(
    rows: Sequence[Mapping[str, str]], tags: tuple[str, ...]
) -> dict[str, float | None]:
    grouped: dict[str, set[float | None]] = {}
    for row in rows:
        name = _local_element(_row_element(row))
        if name not in tags:
            continue
        values = grouped.setdefault(name, set())
        if row.get("ユニットID") == "JPY":
            values.add(_csv_value(row.get("値")))
    return {
        name: next(iter(values)) if len(values) == 1 else None
        for name, values in grouped.items()
    }


def _single_metric(
    rows: Sequence[Mapping[str, str]], tags: tuple[str, ...]
) -> float | None:
    values = _metric_values(rows, tags)
    # Alias preference is explicit, not dependent on CSV row order. A present
    # but unusable preferred concept is not silently replaced with another one.
    return next((values[tag] for tag in tags if tag in values), None)


def _sum_metric(
    rows: Sequence[Mapping[str, str]], tags: tuple[str, ...]
) -> float | None:
    values = _metric_values(rows, tags)
    if not values or any(value is None for value in values.values()):
        return None
    return sum(value for value in values.values() if value is not None)


def _capex_metric(rows: Sequence[Mapping[str, str]]) -> float | None:
    total = _single_metric(rows, _TAGS["capex_total"])
    if total is not None:
        return total
    tangible = _single_metric(rows, _TAGS["capex_tangible"])
    intangible = _single_metric(rows, _TAGS["capex_intangible"])
    return tangible + intangible if tangible is not None and intangible is not None else None


def _cash_flow_details(rows: Sequence[Mapping[str, str]]) -> dict[str, float | None]:
    fields = (
        "ocf_receivables_cash_effect",
        "ocf_inventories_cash_effect",
        "ocf_payables_cash_effect",
        "ocf_contract_liabilities_cash_effect",
        "ocf_advances_received_cash_effect",
        "ocf_other_payables_cash_effect",
    )
    details = {field: _single_metric(rows, _TAGS[field]) for field in fields}
    ppe = _single_metric(rows, _TAGS["capex_tangible"])
    intangible = _single_metric(rows, _TAGS["capex_intangible"])
    details["capex_ppe_reported"] = abs(ppe) if ppe is not None else None
    details["capex_intangible_reported"] = abs(intangible) if intangible is not None else None
    return details


def _debt_metric(rows: Sequence[Mapping[str, str]]) -> float | None:
    total = _single_metric(rows, _TAGS["debt_total"])
    return total if total is not None else _sum_metric(rows, _TAGS["debt_components"])


def _row_element(row: Mapping[str, str]) -> str:
    return row.get("要素ID", "")


def _row_context(row: Mapping[str, str]) -> str:
    return row.get("コンテキストID", "")


def _local_element(value: str) -> str:
    return "".join(char for char in value.rsplit(":", 1)[-1].lower() if char.isalnum())


def _element_matches(element: str, tags: tuple[str, ...]) -> bool:
    return _local_element(element) in tags


def _csv_value(raw: str | None) -> float | None:
    # EDINET CSV defines only the ASCII hyphen as explicit zero.
    return 0.0 if raw is not None and raw.strip() == "-" else to_float(raw)
