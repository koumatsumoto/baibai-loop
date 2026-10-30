"""少数の公開文型に限定した意味抽出。記事ID・受領順・見出しで意味を決めない。"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime
from decimal import Decimal

from baibai_engine.machine_analysis.observations.trends.contracts import (
    Claim,
    ClaimScope,
    DocumentText,
    ExtractionArtifact,
    Quantity,
    Span,
)

SCOPE_REVISION = "industry-scopes-v1"
TSR_REVISION = "tsr-performance-v1"
BOJ_REVISION = "boj-policy-decision-v1"
INDUSTRIES = {
    "医薬品・医療用品卸売業": "jp-pharmaceutical-wholesale",
    "情報サービス業": "jp-information-services",
}
_NUMBER = r"[0-9][0-9,]*(?:\.[0-9]+)?"
_MONEY = rf"(?:{_NUMBER}兆)?(?:{_NUMBER}億)?(?:{_NUMBER}万)?(?:{_NUMBER})?円"


def _span(match: re.Match[str]) -> Span:
    return Span(start=match.start(), end=match.end())


def _number(value: str) -> Decimal:
    return Decimal(value.replace(",", ""))


def _money(value: str) -> Quantity:
    total = Decimal(0)
    resolution = Decimal(1)
    for match in re.finditer(rf"({_NUMBER})(兆|億|万|円)", value):
        number, suffix = match.groups()
        multiplier = {"兆": 10**12, "億": 10**8, "万": 10**4, "円": 1}[suffix]
        amount = _number(number)
        total += amount * multiplier
        resolution = Decimal(10) ** int(amount.as_tuple().exponent) * multiplier
    return Quantity(value=total, unit="JPY", resolution=resolution)


def _artifact(
    document: DocumentText,
    *,
    family: str,
    revision: str,
    created_at: datetime,
    claims: list[Claim],
    limitations: tuple[str, ...],
    supported: bool,
) -> ExtractionArtifact:
    artifact = ExtractionArtifact(
        evidence=document.evidence,
        method="parser",
        method_revision=revision,
        family=family,
        created_at=created_at,
        status=("extracted" if claims else "failed") if supported else "unsupported",
        claims=tuple(claims),
        limitations=limitations,
    )
    artifact.verify_text(document)
    return artifact


def extract_tsr_performance(document: DocumentText, *, created_at: datetime) -> ExtractionArtifact:
    """調査方法段落と一致する対象N社の業績だけ。暦年倒産は別母集団。"""
    text = document.text
    method = re.search(
        r"日本産業分類.{0,30}?「(?P<label>[^」]+)」.{0,30}?"
        r"(?P<year>20\d{2})年度.{0,30}?\((?P<start_year>20\d{2})年"
        r"(?P<start_month>\d{1,2})月期[~〜～](?P<end_year>20\d{2})年"
        r"(?P<end_month>\d{1,2})月期.{0,100}?(?P<size>[\d,]+)社を抽出、分析した",
        text,
    )
    supported = (
        document.evidence.source == "tsr"
        and document.evidence.content_scope in ("full_text", "partial_text")
        and method is not None
        and method["label"] in INDUSTRIES
    )
    claims: list[Claim] = []
    limits = (
        "tsr_performance_survey_only",
        "calendar_year_bankruptcies_are_not_the_survey_population",
        "profitability_share_is_not_profit_margin",
        "unparsed_claims_and_figures_are_not_absent_events",
    )
    if not supported or method is None:
        return _artifact(
            document,
            family="tsr_performance",
            revision=TSR_REVISION,
            created_at=created_at,
            claims=claims,
            limitations=limits,
            supported=False,
        )
    year = int(method["year"])
    size = int(method["size"].replace(",", ""))
    end_year, end_month = int(method["end_year"]), int(method["end_month"])
    scope = ClaimScope(
        scope_id=INDUSTRIES[method["label"]],
        taxonomy_revision=SCOPE_REVISION,
        label=method["label"],
        geography="JP",
        population="TSR survey: five consecutive comparable fiscal years",
        population_id=f"tsr-survey:{document.evidence.article_id}",
        population_size=size,
        period=f"FY{year}",
        period_start=date(int(method["start_year"]), int(method["start_month"]), 1),
        period_end=date(end_year, end_month, calendar.monthrange(end_year, end_month)[1]),
        period_basis="fiscal_year_end_months",
    )
    # The size/year anchor prevents nearby calendar-year exits or a large-company
    # subgroup from being attributed to the entire survey.
    size_pattern = re.escape(method["size"])
    revenue = re.search(
        rf"{size_pattern}社の{year}年度の(?:業績は、)?売上高(?:合計)?(?:は|が)、?"
        rf"(?P<value>{_MONEY})",
        text,
    )
    if revenue:
        claims.append(
            Claim(
                claim_id="revenue",
                domain="industry",
                kind="reported_fact",
                predicate="revenue",
                scope=scope,
                attribution="東京商工リサーチ",
                quantity=_money(revenue["value"]),
                spans=(_span(method), _span(revenue)),
            )
        )
        # Only the same sentence's explicitly named net income; generic 利益金
        # has no known basis and must not silently become net income.
        stop = text.find("。", revenue.end())
        profit = re.search(rf"当期利益は(?P<value>{_MONEY})", text[revenue.end() : stop])
        if profit:
            claims.append(
                Claim(
                    claim_id="net_income",
                    domain="industry",
                    kind="reported_fact",
                    predicate="net_income",
                    scope=scope,
                    attribution="東京商工リサーチ",
                    quantity=_money(profit["value"]).model_copy(update={"basis": "net_income"}),
                    spans=(
                        _span(method),
                        Span(
                            start=revenue.end() + profit.start(),
                            end=revenue.end() + profit.end(),
                        ),
                    ),
                )
            )
    share = re.search(
        rf"{size_pattern}社の{year}年度の損益別は、黒字が[\d,]+社"
        rf"\(前年度[\d,]+社\)で(?P<current>{_NUMBER})%"
        rf"\(前年度(?P<previous>{_NUMBER})%\)",
        text,
    )
    if share:
        for key, claim_year in (("current", year), ("previous", year - 1)):
            prior = key == "previous"
            claim_scope = (
                scope
                if not prior
                else scope.model_copy(
                    update={
                        "period": f"FY{claim_year}",
                        "period_start": scope.period_start.replace(year=scope.period_start.year - 1)
                        if scope.period_start
                        else None,
                        "period_end": date(
                            end_year - 1, end_month, calendar.monthrange(end_year - 1, end_month)[1]
                        ),
                    }
                )
            )
            amount = _number(share[key])
            claims.append(
                Claim(
                    claim_id=f"profitable_share_{claim_year}",
                    domain="industry",
                    kind="reported_fact",
                    predicate="profitable_company_share",
                    scope=claim_scope,
                    attribution="東京商工リサーチ",
                    spans=(_span(method), _span(share)),
                    quantity=Quantity(
                        value=amount,
                        unit="percent",
                        resolution=Decimal(10) ** int(amount.as_tuple().exponent),
                        basis="companies_with_positive_reported_profit",
                    ),
                )
            )
    # A concentration is an explicitly reported business structure, not a
    # conclusion about competition, supply loss, or investment profitability.
    if revenue:
        concentrations = (
            (
                (
                    r"売上高(?P<threshold>[\d,]+)億円(?P<bound>超|以上)の大手は[\d,]+社"
                    r"[^。]{0,160}?シェアは売上高の(?P<share>[\d.]+)%"
                ),
                "revenue",
            ),
            (
                (
                    r"売上高も、(?P<threshold>[\d,]+)人(?P<bound>以上)の企業が全体の"
                    r"(?P<share>[\d.]+)%"
                ),
                "employees",
            ),
        )
        for expression, basis in concentrations:
            concentration = re.search(expression, text)
            if concentration is None:
                continue
            threshold = concentration["threshold"].replace(",", "")
            target = f"{basis}:{threshold}:{concentration['bound']}"
            if basis == "revenue":
                target += ":JPY_100_million"
            amount = _number(concentration["share"])
            claims.append(
                Claim(
                    claim_id=f"revenue_concentration_{basis}",
                    domain="industry",
                    kind="reported_fact",
                    predicate="revenue_concentration",
                    scope=scope.model_copy(update={"target": target}),
                    attribution="東京商工リサーチ",
                    spans=(_span(method), _span(concentration)),
                    quantity=Quantity(
                        value=amount,
                        unit="percent",
                        resolution=Decimal(10) ** int(amount.as_tuple().exponent),
                        basis="subgroup_share_of_survey_revenue",
                    ),
                )
            )
    return _artifact(
        document,
        family="tsr_performance",
        revision=TSR_REVISION,
        created_at=created_at,
        claims=claims,
        limitations=limits,
        supported=True,
    )


def extract_boj_decision(document: DocumentText, *, created_at: datetime) -> ExtractionArtifact:
    """最初の採決済み調節方針とその脚注。否決案・別制度金利は採用しない。"""
    text = document.text
    decision = re.search(
        r"日本銀行は、本日、.{0,50}?次回金融政策決定会合までの金融市場調節方針を、"
        r"以下のとおりとすることを決定した.{0,80}?"
        r"無担保コールレート\(オーバーナイト物\)を、?"
        rf"(?P<rate>{_NUMBER})%(?P<approx>程度)?で推移するよう促す(?P<note>\d*)。",
        text,
    )
    supported = (
        document.evidence.source == "boj"
        and document.evidence.content_scope in ("full_text", "partial_text")
        and decision is not None
    )
    claims: list[Claim] = []
    limits: tuple[str, ...] = (
        "boj_overnight_call_decision_only",
        "scheduled_date_is_not_implementation_evidence",
    )
    if not supported or decision is None:
        return _artifact(
            document,
            family="boj_policy_decision",
            revision=BOJ_REVISION,
            created_at=created_at,
            claims=claims,
            limitations=limits,
            supported=False,
        )
    heading_date = re.search(
        r"(?P<year>20\d{2})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日", text[: decision.start()]
    )
    occurred = (
        None
        if heading_date is None
        else date(
            int(heading_date["year"]),
            int(heading_date["month"]),
            int(heading_date["day"]),
        )
    )
    scope = ClaimScope(
        scope_id="jp-monetary-policy",
        taxonomy_revision="social-scopes-v1",
        label="日本銀行の金融市場調節方針",
        geography="JP",
        target="uncollateralized_overnight_call_rate",
    )
    rate = _number(decision["rate"])
    quantity = Quantity(
        value=rate,
        unit="percent",
        resolution=Decimal(10) ** int(rate.as_tuple().exponent),
        approximate=bool(decision["approx"]),
        basis="overnight_call_rate_target",
    )
    claims.append(
        Claim(
            claim_id="overnight_call_rate_decision",
            domain="social",
            kind="reported_fact",
            predicate="overnight_call_rate_target",
            scope=scope,
            attribution="日本銀行",
            status="decided",
            occurred_on=occurred,
            quantity=quantity,
            spans=(_span(decision),),
            conditions=("until_next_monetary_policy_meeting",),
        )
    )
    # Require both a decision marker and the footnote's explicit subject. An
    # unrelated facility's effective date, even if identical, cannot fill it.
    if decision["note"]:
        note = re.search(
            re.escape(decision["note"])
            + r"新たな金融市場調節方針は、翌営業日\((?P<month>\d{1,2})月"
            r"(?P<day>\d{1,2})日\)から適用する。",
            text,
        )
        if note and occurred:
            month, day = int(note["month"]), int(note["day"])
            # A December/January boundary is not inferred from a missing year.
            scheduled = date(occurred.year, month, day) if month >= occurred.month else None
            claims.append(
                Claim(
                    claim_id="overnight_call_rate_schedule",
                    domain="social",
                    kind="announced_plan",
                    predicate="overnight_call_rate_target",
                    scope=scope,
                    attribution="日本銀行",
                    status="scheduled",
                    occurred_on=occurred,
                    scheduled_for=scheduled,
                    quantity=quantity,
                    spans=(_span(decision), _span(note)),
                    uncertainty=() if scheduled else ("effective_year_unknown",),
                    conditions=("until_next_monetary_policy_meeting",),
                )
            )
        else:
            limits += ("decision_footnote_not_resolved",)
    return _artifact(
        document,
        family="boj_policy_decision",
        revision=BOJ_REVISION,
        created_at=created_at,
        claims=claims,
        limitations=limits,
        supported=True,
    )


def extract_supported_family(document: DocumentText, *, created_at: datetime) -> ExtractionArtifact:
    if document.evidence.source == "tsr":
        return extract_tsr_performance(document, created_at=created_at)
    if document.evidence.source == "boj":
        return extract_boj_decision(document, created_at=created_at)
    return _artifact(
        document,
        family="unsupported",
        revision="supported-families-v1",
        created_at=created_at,
        claims=[],
        limitations=("no_supported_parser_family",),
        supported=False,
    )
