"""EDINET CSV-ZIP financial statement parsing into metric records.

Extracts current statement balances and flows with their reported-period quality,
without annualizing interim statements. This is pure parsing over downloaded bytes,
separate from the API provider.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Literal

from baibai_engine.primitives.dates import add_months_clamped
from baibai_engine.primitives.time import JST
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
    rows: Sequence[Mapping[str, str]] = _read_csv_zip_rows(content)
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

    quality = _flow_period_quality(doc_type_code, period_start, period_end, submit_datetime)
    consolidation_dei = _consolidation_dei_status(rows)
    rows, accounting_failure = _accounting_standard_rows(rows)
    rows, basis = _select_statement_rows(
        rows,
        doc_type_code,
        allow_non_consolidated=consolidation_dei in {"false", "missing"},
    )
    if any(_row_element(row) in _IFRS_ALIASES for row in rows) and any(
        _row_element(row) not in _IFRS_ALIASES for row in rows
    ):
        rows, basis = [], None
        accounting_failure = "accounting_standard_dei_missing_for_mixed_facts"
    failures: list[str] = [accounting_failure] if accounting_failure is not None else []
    if consolidation_dei == "true" and basis is None:
        failures.append("consolidated_dei_no_supported_current_facts")
    elif consolidation_dei in {"conflict", "invalid"}:
        failures.append(f"consolidation_dei_{consolidation_dei}")
    elif consolidation_dei == "missing" and basis == "non_consolidated":
        failures.append("consolidation_dei_missing")
    if basis == "non_consolidated":
        failures.append("non_consolidated_fallback")

    sales = _single_metric(rows, _TAGS["sales"])
    ocf = _single_metric(rows, _TAGS["ocf"])
    operating_profit = _single_metric(rows, _TAGS["operating_profit"])
    cash = _single_metric(rows, _TAGS["cash"])
    investment_securities = _single_metric(rows, _TAGS["investment_securities"])
    equity = _single_metric(rows, _TAGS["equity"])
    total_assets = _single_metric(rows, _TAGS["total_assets"])
    debt = _sum_metric(rows, _TAGS["debt_components"])
    depreciation = _depreciation_metric(rows)
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
        ttm_quality_net_cash=TTMQuality.EXACT if net_cash is not None else TTMQuality.UNAVAILABLE,
        source_doc_id=doc_id,
        document_type=doc_type_code,
        source_submit_datetime=submit_datetime,
        source_period_start=period_start,
        source_period_end=period_end,
        capex_source="purchase_of_fixed_assets" if capex_abs is not None else None,
        failure_reasons=tuple(dict.fromkeys(failures)),
    )


# Only standard IFRS concepts whose statement meaning has been checked against
# native CSV/XBRL are aliases. Issuer extensions and similarly named totals are
# not inferred. In particular, bonds/borrowings alone do not establish complete
# financial debt, and other financial assets are not investment securities.
_IFRS_ALIASES = {
    "jpigp_cor:RevenueIFRS": "jppfs_cor:NetSales",
    "jpigp_cor:NetSalesIFRS": "jppfs_cor:NetSales",
    "jpigp_cor:NetCashProvidedByUsedInOperatingActivitiesIFRS": (
        "jppfs_cor:NetCashProvidedByUsedInOperatingActivities"
    ),
    "jpigp_cor:OperatingProfitLossIFRS": "jppfs_cor:OperatingIncome",
    "jpigp_cor:CashAndCashEquivalentsIFRS": "jppfs_cor:CashAndCashEquivalents",
    "jpigp_cor:EquityIFRS": "jppfs_cor:NetAssets",
    "jpigp_cor:AssetsIFRS": "jppfs_cor:Assets",
    "jpigp_cor:DepreciationAndAmortizationOpeCFIFRS": "jppfs_cor:DepreciationAndAmortizationOpeCF",
    "jpigp_cor:PurchaseOfPropertyPlantAndEquipmentInvCFIFRS": (
        "jppfs_cor:PurchaseOfPropertyPlantAndEquipmentInvCF"
    ),
    "jpigp_cor:PurchaseOfIntangibleAssetsInvCFIFRS": "jppfs_cor:PurchaseOfIntangibleAssetsInvCF",
}


# Exact qualified standard concepts. Issuer local-name homonyms are not aliases.
_TAGS: dict[str, tuple[str, ...]] = {
    "sales": (
        "jppfs_cor:NetSales",
        "jpcrp_cor:RevenuesFromExternalCustomers",
    ),
    "ocf": ("jppfs_cor:NetCashProvidedByUsedInOperatingActivities",),
    "operating_profit": ("jppfs_cor:OperatingIncome",),
    "cash": (
        "jppfs_cor:CashAndDeposits",
        "jppfs_cor:CashAndCashEquivalents",
    ),
    "investment_securities": ("jppfs_cor:InvestmentSecurities",),
    "equity": ("jppfs_cor:NetAssets",),
    "total_assets": ("jppfs_cor:Assets",),
    "debt_components": (
        "jppfs_cor:ShortTermLoansPayable",
        "jppfs_cor:CurrentPortionOfLongTermLoansPayable",
        "jppfs_cor:CurrentPortionOfBonds",
        "jppfs_cor:BondsPayable",
        "jppfs_cor:ConvertibleBondTypeBondsWithSubscriptionRightsToShares",
        "jppfs_cor:LongTermLoansPayable",
        "jppfs_cor:LeaseObligationsCL",
        "jppfs_cor:LeaseObligationsNCL",
    ),
    "depreciation": (
        "jppfs_cor:DepreciationAndAmortizationOpeCF",
        "jppfs_cor:AmortizationOfGoodwillOpeCF",
    ),
    "capex_total": ("jppfs_cor:PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssetsInvCF",),
    "capex_tangible": ("jppfs_cor:PurchaseOfPropertyPlantAndEquipmentInvCF",),
    "capex_intangible": ("jppfs_cor:PurchaseOfIntangibleAssetsInvCF",),
    "ocf_receivables_cash_effect": (
        "jppfs_cor:DecreaseIncreaseInNotesAndAccountsReceivableTradeOpeCF",
    ),
    "ocf_inventories_cash_effect": ("jppfs_cor:DecreaseIncreaseInInventoriesOpeCF",),
    "ocf_payables_cash_effect": ("jppfs_cor:IncreaseDecreaseInNotesAndAccountsPayableTradeOpeCF",),
    "ocf_contract_liabilities_cash_effect": (
        "jppfs_cor:IncreaseDecreaseInContractLiabilitiesOpeCF",
    ),
    "ocf_advances_received_cash_effect": ("jppfs_cor:IncreaseDecreaseInAdvancesReceivedOpeCF",),
    "ocf_other_payables_cash_effect": ("jppfs_cor:IncreaseDecreaseInAccountsPayableOtherOpeCF",),
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


def _flow_period_quality(
    doc_type_code: str,
    period_start: date | None,
    period_end: date | None,
    submit_datetime: str | None,
) -> TTMQuality:
    # A short fiscal year is still an annual filing, not a twelve-month flow.
    # Interim document metadata can describe the full fiscal year, so it never
    # upgrades an InterimDuration fact to exact TTM.
    if doc_type_code not in {"120", "130"} or period_start is None or period_end is None:
        return TTMQuality.APPROXIMATED
    if period_end <= period_start or period_end.year <= 1:
        return TTMQuality.APPROXIMATED
    if submit_datetime is not None:
        try:
            submitted = datetime.fromisoformat(submit_datetime)
        except ValueError:
            return TTMQuality.APPROXIMATED
        # EDINET's native timezone-free submission time is JST. Offset-bearing
        # values are compared on that same calendar date, not their written date.
        submitted_on = (
            submitted.date() if submitted.tzinfo is None else submitted.astimezone(JST).date()
        )
        if period_end > submitted_on:
            return TTMQuality.APPROXIMATED
    annual_start = add_months_clamped(period_end, -12) + timedelta(days=1)
    return TTMQuality.EXACT if period_start == annual_start else TTMQuality.APPROXIMATED


_ConsolidationDEIStatus = Literal["true", "false", "missing", "conflict", "invalid"]
_CONSOLIDATION_DEI_ELEMENT = "jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI"


def _consolidation_dei_status(rows: Sequence[Mapping[str, str]]) -> _ConsolidationDEIStatus:
    facts = [
        row
        for row in rows
        if _row_element(row) == _CONSOLIDATION_DEI_ELEMENT
        and _row_context(row) == "FilingDateInstant"
    ]
    if not facts:
        return "missing"
    if any(
        row.get("ユニットID") != "－" or row.get("値") not in {"true", "false"} for row in facts
    ):
        return "invalid"
    values = {row["値"] for row in facts}
    return "conflict" if len(values) > 1 else "true" if "true" in values else "false"


def _select_statement_rows(
    rows: Sequence[Mapping[str, str]],
    doc_type_code: str,
    *,
    allow_non_consolidated: bool = True,
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
    if not allow_non_consolidated:
        return [], None
    individual_contexts = {f"{context}_NonConsolidatedMember" for context in contexts}
    individual = [row for row in financial if _row_context(row) in individual_contexts]
    return individual, "non_consolidated" if individual else None


def _accounting_standard_rows(
    rows: Sequence[Mapping[str, str]],
) -> tuple[Sequence[Mapping[str, str]], str | None]:
    """A filing may show IFRS and Japanese GAAP in the very same context.

    Select the filing's declared standard before choosing consolidated/individual
    contexts. Otherwise identical metric aliases falsely conflict, or amounts
    from different accounting standards get combined into one balance/flow.
    """
    facts = [
        row
        for row in rows
        if _row_element(row) == "jpdei_cor:AccountingStandardsDEI"
        and _row_context(row) == "FilingDateInstant"
    ]
    if not facts:
        return rows, None
    if any(row.get("ユニットID") != "－" or not row.get("値") for row in facts):
        return [], "accounting_standard_dei_invalid"
    standards = {row["値"] for row in facts}
    if len(standards) != 1:
        return [], "accounting_standard_dei_conflict"
    standard = next(iter(standards))
    if standard == "IFRS":
        return [row for row in rows if _row_element(row) in _IFRS_ALIASES], None
    if standard == "Japan GAAP":
        return [row for row in rows if not _row_element(row).startswith("jpigp_cor:")], None
    return [], "accounting_standard_dei_unsupported"


def _metric_values(
    rows: Sequence[Mapping[str, str]], tags: tuple[str, ...]
) -> dict[str, float | None]:
    grouped: dict[str, set[float | None]] = {}
    for row in rows:
        name = _metric_element(_row_element(row))
        if name not in tags:
            continue
        values = grouped.setdefault(name, set())
        if row.get("ユニットID") == "JPY":
            values.add(_csv_value(row.get("値")))
    return {
        name: next(iter(values)) if len(values) == 1 else None for name, values in grouped.items()
    }


def _single_metric(rows: Sequence[Mapping[str, str]], tags: tuple[str, ...]) -> float | None:
    values = _metric_values(rows, tags)
    # Alias preference is explicit, not dependent on CSV row order. A present
    # but unusable preferred concept is not silently replaced with another one.
    return next((values[tag] for tag in tags if tag in values), None)


def _sum_metric(rows: Sequence[Mapping[str, str]], tags: tuple[str, ...]) -> float | None:
    values = _metric_values(rows, tags)
    if not values or any(value is None for value in values.values()):
        return None
    return sum(value for value in values.values() if value is not None)


def _depreciation_metric(rows: Sequence[Mapping[str, str]]) -> float | None:
    values = _metric_values(rows, _TAGS["depreciation"])
    depreciation = values.get("jppfs_cor:DepreciationAndAmortizationOpeCF")
    goodwill = values.get("jppfs_cor:AmortizationOfGoodwillOpeCF", 0.0)
    # Goodwill amortization alone cannot stand in for depreciation. Add it when
    # reported; this does not establish completeness of all amortization charges.
    return depreciation + goodwill if depreciation is not None and goodwill is not None else None


def _capex_metric(rows: Sequence[Mapping[str, str]]) -> float | None:
    totals = _metric_values(rows, _TAGS["capex_total"])
    if totals:
        # A reported but unresolved total is not an absent total. Do not hide
        # conflicting, missing or non-JPY source values behind component sums.
        return next(iter(totals.values()))
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


def _row_element(row: Mapping[str, str]) -> str:
    return row.get("要素ID", "")


def _row_context(row: Mapping[str, str]) -> str:
    return row.get("コンテキストID", "")


def _metric_element(value: str) -> str:
    return _IFRS_ALIASES.get(value, value)


def _element_matches(element: str, tags: tuple[str, ...]) -> bool:
    return _metric_element(element) in tags


def _csv_value(raw: str | None) -> float | None:
    # EDINET CSV defines only the ASCII hyphen as explicit zero.
    return 0.0 if raw is not None and raw.strip() == "-" else to_float(raw)
