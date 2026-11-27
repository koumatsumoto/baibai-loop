"""Reviewed USD analysis and non-executable US investment decisions."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from baibai_engine.data_collection.providers.alpaca_auctions import (
    NasdaqOfficialClose,
    NasdaqOfficialCloseQuery,
    SipAuctionReceipt,
    read_nasdaq_official_close,
)
from baibai_engine.investment_judgment.contracts import PerspectiveAssessment, PerspectiveModel
from baibai_engine.primitives.json import canonical_json
from baibai_engine.primitives.numbers import finite_decimal
from baibai_engine.storage.sqlite.application.write import connect_existing

_ET = ZoneInfo("America/New_York")
_EXCHANGE_HOST = {"XNAS": "nasdaq.com", "XNYS": "nyse.com"}

type Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
type ReportId = Annotated[str, Field(pattern=r"^us-report-[a-z0-9-]{1,64}$")]
type Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type USD = Annotated[Decimal, BeforeValidator(finite_decimal), Field(ge=0, max_digits=16)]
type Price = Annotated[USD, Field(gt=0)]


def _exact_report_clock(value: object) -> datetime:
    if isinstance(value, datetime):
        stamp = value
    elif (
        isinstance(value, str)
        and not value.endswith("-00:00")
        and re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
            r"(?:\.[0-9]{1,6}0*)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
            value,
        )
    ):
        stamp = datetime.fromisoformat(value)
    else:
        raise ValueError("report requires an exact, offset-aware time")
    offset = stamp.utcoffset()
    if offset is None:
        raise ValueError("report requires an exact, offset-aware time")
    try:
        utc = stamp.astimezone(UTC)
    except OverflowError as exc:
        raise ValueError("observation time outside representable UTC range") from exc
    # Preserve the serialized offset that existing review digests bind. A fixed offset
    # also compares the two occurrences of a local DST hour as distinct instants.
    # Sub-minute offsets cannot round-trip in Pydantic JSON; keep that instant in UTC.
    return (
        utc
        if offset.seconds % 60 or offset.microseconds
        else stamp.replace(tzinfo=timezone(offset))
    )


type _ReportClock = Annotated[AwareDatetime, BeforeValidator(_exact_report_clock)]


def _exchange_date(stamp: datetime) -> date:
    """A UTC-representable instant may still fall before the first New York date."""
    try:
        return stamp.astimezone(_ET).date()
    except OverflowError as exc:
        raise ValueError("clock has no representable New York date") from exc


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _IEXBand(_Record):
    feed: Literal["alpaca_iex"]
    basis: Literal["split_adjusted_closes"]
    manifest_sha256: Digest
    received_at: _ReportClock
    first_session_et: date
    last_session_et: date
    observations: Annotated[int, Field(strict=True, ge=2)]
    low_usd: Price
    high_usd: Price
    last_usd: Price

    @model_validator(mode="after")
    def _window(self) -> Self:
        if (
            self.observations > (self.last_session_et - self.first_session_et).days + 1
            or not self.low_usd <= self.last_usd <= self.high_usd
            or _exchange_date(self.received_at) <= self.last_session_et
        ):
            raise ValueError("invalid split-adjusted IEX observation window")
        return self


class _UnadjustedClose(_Record):
    basis: Literal["regular_session_unadjusted"]
    session_et: date
    price_usd: Price
    source_url: HttpUrl
    observed_at: _ReportClock


class _OfficialClose(_UnadjustedClose):
    """The original direct listing-exchange evidence shape."""

    origin: ClassVar[Literal["listing_exchange"]] = "listing_exchange"


class _NasdaqSipClose(_UnadjustedClose):
    origin: Literal["alpaca_sip_nasdaq_official_close"]
    venue: Literal["XNAS"]
    symbol: Text
    price_unit: Literal["USD_per_share"]
    native_response_sha256: Digest
    source_json_pointer: Text
    event_at_rfc3339: Text
    exchange_code: Literal["Q"]
    sale_condition: Literal["M"]


class _FinancialEvidence(_Record):
    source_url: HttpUrl
    period_end: date
    observed_at: _ReportClock
    fact_and_scope: Text


class _Scenario(_Record):
    terminal_value_usd_per_share: USD
    eligible_distributions_usd_per_share: USD
    assumptions: Annotated[tuple[Text, ...], Field(min_length=1)]

    @property
    def total(self) -> Decimal:
        return self.terminal_value_usd_per_share + self.eligible_distributions_usd_per_share

    def difference_from_cash(self, cash_usd_per_share: Decimal) -> Decimal:
        return self.total - cash_usd_per_share


class _Comparison(_Record):
    horizon_et: date
    cash_basis: Literal["same_usd_no_interest_tax_or_fees"]
    base: _Scenario
    downside: _Scenario


def _public_https(url: HttpUrl) -> bool:
    return url.scheme == "https" and url.username is None and url.password is None


def _listing_https(url: HttpUrl, venue: str) -> bool:
    host, listing_host = url.host or "", _EXCHANGE_HOST[venue]
    return _public_https(url) and (host == listing_host or host.endswith("." + listing_host))


class ReportDraft(_Record):
    kind: Literal["us_stock_analytical_report"]
    report_id: ReportId
    author_identity: Text
    venue: Literal["XNAS", "XNYS"]
    ticker: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9]*(?:\.[A-Z0-9]+)?$", max_length=8)]
    cik: Annotated[str, Field(min_length=10, max_length=10, pattern=r"^0*[1-9][0-9]*$")]
    share_class: Text
    currency: Literal["USD"]
    cutoff_at: _ReportClock
    prepared_at: _ReportClock
    expected_session_et: date
    session_close_at: _ReportClock
    calendar_source_url: HttpUrl
    iex_band: _IEXBand | None
    official_close: _OfficialClose | _NasdaqSipClose | None
    financial_evidence: Annotated[tuple[_FinancialEvidence, ...], Field(min_length=1)]
    thesis: Text
    strongest_countercase: Text
    comparison: _Comparison | None
    unresolved: tuple[Text, ...]

    @model_validator(mode="after")
    def _evidence(self) -> Self:
        cutoff_et = _exchange_date(self.cutoff_at)
        if (
            self.prepared_at < self.cutoff_at
            or self.session_close_at > self.cutoff_at
            or _exchange_date(self.session_close_at) != self.expected_session_et
            or not _listing_https(self.calendar_source_url, self.venue)
        ):
            raise ValueError("invalid report cutoff or calendar citation")
        if any(
            fact.observed_at > self.cutoff_at
            or fact.period_end > _exchange_date(fact.observed_at)
            or not _public_https(fact.source_url)
            for fact in self.financial_evidence
        ):
            raise ValueError("financial evidence is unavailable at cutoff")
        band = self.iex_band
        if band is not None and (
            band.last_session_et != self.expected_session_et or band.received_at > self.cutoff_at
        ):
            raise ValueError("IEX window does not establish the expected session at cutoff")
        close = self.official_close
        if close is not None:
            if (
                close.session_et != self.expected_session_et
                or not self.session_close_at <= close.observed_at <= self.cutoff_at
            ):
                raise ValueError(
                    "official observation requires a matching closed session at cutoff"
                )
            if isinstance(close, _NasdaqSipClose):
                if (
                    close.venue != self.venue
                    or close.symbol != self.ticker
                    or not _public_https(close.source_url)
                ):
                    raise ValueError(
                        "relayed official observation requires the matching XNAS listing"
                    )
            elif not _listing_https(close.source_url, self.venue):
                raise ValueError(
                    "official observation requires a matching listing-exchange citation"
                )
        comparison = self.comparison
        if comparison is not None and (
            close is None
            or comparison.horizon_et <= cutoff_et
            or comparison.downside.total > comparison.base.total
        ):
            raise ValueError("cash comparison requires an official close and coherent scenarios")
        if (band is None or comparison is None) and not self.unresolved:
            raise ValueError("missing price or comparison requires an explicit limitation")
        return self


@dataclass(frozen=True, slots=True)
class SipCloseProof:
    """Separate capture inputs, never proof manufactured by the report author."""

    query: NasdaqOfficialCloseQuery
    receipt: SipAuctionReceipt


def sip_official_close_fields(observation: NasdaqOfficialClose) -> dict[str, object]:
    """Project the validated provider observation without altering its precision or source."""
    return _NasdaqSipClose.model_validate(observation, from_attributes=True).model_dump(mode="json")


def require_close_receipt(draft: ReportDraft, sip_proof: SipCloseProof | None = None) -> None:
    """Admit a new SIP judgment by replaying its receipt, not by trusting a typed claim."""
    close = draft.official_close
    if not isinstance(close, _NasdaqSipClose):
        if sip_proof is not None:
            raise ValueError("SIP receipt requires the explicit relayed close variant")
        return
    if sip_proof is None:
        raise ValueError("relayed close requires its original SIP receipt")
    if sip_proof.query.symbol != draft.ticker:
        raise ValueError("SIP receipt requires the report's literal ticker")
    observed = read_nasdaq_official_close(
        sip_proof.query,
        sip_proof.receipt,
        listing_venue=draft.venue,
        session_et=draft.expected_session_et,
        session_close_at=draft.session_close_at,
        cutoff_at=draft.cutoff_at,
    )
    if sip_official_close_fields(observed) != close.model_dump(mode="json"):
        raise ValueError("relayed close differs from the accepted original SIP observation")


class ReportReview(_Record):
    report_id: ReportId
    draft_sha256: Digest
    reviewer_identity: Text
    reviewed_at: _ReportClock
    conclusion: Literal["pass"]
    notes: Text


class ReviewedReport(_Record):
    draft: ReportDraft
    review: ReportReview
    rendered_sha256: Digest


def _digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def draft_digest(draft: ReportDraft) -> str:
    return _digest(canonical_json(draft.model_dump(mode="json")).encode())


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _line(text: str) -> str:
    return _one_line(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _bind_report_review(draft: ReportDraft, review: ReportReview) -> str:
    digest = draft_digest(draft)
    author_name = _one_line(draft.author_identity).casefold()
    reviewer_name = _one_line(review.reviewer_identity).casefold()
    if (
        review.report_id != draft.report_id
        or review.draft_sha256 != digest
        or reviewer_name == author_name
        or review.reviewed_at < draft.prepared_at
    ):
        raise ValueError("review does not bind this exact independent draft")
    return digest


def render(draft: ReportDraft, review: ReportReview) -> bytes:
    digest = _bind_report_review(draft, review)
    lines = [
        f"# {draft.ticker} 米国株 調査・比較（正式提案未発行）",
        "",
        f"- レポートID: {draft.report_id}",
        f"- 原稿SHA-256: {digest}",
        f"- 対象: {draft.venue} / {_line(draft.share_class)} / CIK {draft.cik}",
        f"- 情報締切: {draft.cutoff_at.isoformat()}、対象立会日（ET）: {draft.expected_session_et}",
        f"- 対象立会の終了: {draft.session_close_at.isoformat()}",
        f"- 日付・時刻根拠: {draft.calendar_source_url}",
        "- USD建ての資料。口座・保有・注文可否・執行条件を示しません。",
        "",
        "## 観測価格と評価",
    ]
    band = draft.iex_band
    if band is None:
        lines.append("- IEX観測帯: 未確認。")
    else:
        lines.extend(
            [
                (
                    f"- Alpaca IEX・分割調整済み終値 {band.observations} 観測"
                    f"（{band.first_session_et} ～ {band.last_session_et} ET）: "
                    f"USD {band.low_usd} ～ {band.high_usd} / 株、末尾 USD {band.last_usd} / 株。"
                ),
                f"- IEX受領: {band.received_at.isoformat()}",
                f"- 受領manifest SHA-256: {band.manifest_sha256}",
            ]
        )
    close = draft.official_close
    if close is None:
        lines.append("- 上場取引所の無調整終値: 未確認。IEXで代用していません。")
    elif isinstance(close, _NasdaqSipClose):
        lines.extend(
            [
                (
                    f"- Nasdaqの無調整公式終値（Alpaca SIP経由）: USD {close.price_usd} / 株 "
                    f"（{close.session_et} ET、受領 {close.observed_at.isoformat()}）: "
                    f"{close.source_url}"
                ),
                (
                    f"- Q/M: {close.event_at_rfc3339}、"
                    f"原本SHA-256: {close.native_response_sha256}、"
                    f"該当箇所: {close.source_json_pointer}"
                ),
            ]
        )
    else:
        lines.append(
            f"- 上場取引所の無調整終値: USD {close.price_usd} / 株 "
            f"（{close.session_et} ET、取得 {close.observed_at.isoformat()}）: {close.source_url}"
        )
    lines.extend(["", "## 財務・事業の根拠"])
    for fact in draft.financial_evidence:
        lines.append(
            f"- {_line(fact.fact_and_scope)}（期末 {fact.period_end}、"
            f"取得 {fact.observed_at.isoformat()}）: {fact.source_url}"
        )
    lines.extend(
        [
            f"- 仮説: {_line(draft.thesis)}",
            f"- 最強の反対材料: {_line(draft.strongest_countercase)}",
        ]
    )
    lines.extend(["", "## 同期間のUSD現金との比較"])
    comparison = draft.comparison
    if comparison is None:
        lines.append("- 判断保留: 価格条件または比較の前提が未確認です。")
    else:
        assert close is not None
        lines.append(
            f"- {comparison.horizon_et} ETまで。無利息のUSD現金 USD {close.price_usd} / 株相当。"
            "税・費用・為替は含めず、約定・現金残高を仮定しません。"
        )
        for label, scenario in (("基本", comparison.base), ("下振れ", comparison.downside)):
            lines.append(
                f"- {label}: 終価 USD {scenario.terminal_value_usd_per_share}、"
                f"取得権のある分配 USD {scenario.eligible_distributions_usd_per_share}、"
                f"現金との差 USD {scenario.difference_from_cash(close.price_usd):+f}"
                "（各1株当たりの仮定）。"
            )
            lines.extend(f"  - 仮定: {_line(value)}" for value in scenario.assumptions)
    if draft.unresolved:
        lines.extend(["", "## 不足と見直し条件", *(f"- {_line(x)}" for x in draft.unresolved)])
    lines.extend(
        [
            "",
            f"- 別担当の査読: {_line(review.reviewer_identity)}",
            f"- 査読時刻: {review.reviewed_at.isoformat()}",
            f"- 査読所見: {_line(review.notes)}",
        ]
    )
    return ("\n".join(lines) + "\n").encode()


def reviewed_report(
    draft: ReportDraft, review: ReportReview, *, sip_proof: SipCloseProof | None = None
) -> ReviewedReport:
    require_close_receipt(draft, sip_proof)
    return ReviewedReport(
        draft=draft, review=review, rendered_sha256=_digest(render(draft, review))
    )


def render_reviewed_report(report: ReviewedReport) -> bytes:
    content = render(report.draft, report.review)
    if _digest(content) != report.rendered_sha256:
        raise ValueError("saved render digest differs")
    return content


type USProposalId = Annotated[str, Field(pattern=r"^us-proposal-[a-z0-9-]{1,64}$")]
_US_PROPOSAL_ID: TypeAdapter[str] = TypeAdapter(USProposalId)


class _PerspectiveSource(_Record):
    """A public, human-findable citation. The private source document is not a Web field."""

    label: Annotated[Text, Field(max_length=180)]
    observed_at: _ReportClock

    @field_validator("label")
    @classmethod
    def _no_raw_url(cls, value: str) -> str:
        if "://" in value:
            raise ValueError("cite the public document title and section, not its raw URL")
        return value

    @field_validator("observed_at")
    @classmethod
    def _source_clock(cls, stamp: datetime) -> datetime:
        return stamp.astimezone(UTC)


class _USProposalPerspective(_Record):
    model: PerspectiveModel
    assessment: PerspectiveAssessment
    # Twenty complete decisions share one serving object. Publish a brief and key citations,
    # not all underlying research; the private report is unaffected by these limits.
    summary: Annotated[Text, Field(max_length=480)]
    sources: Annotated[tuple[_PerspectiveSource, ...], Field(max_length=4)]

    @model_validator(mode="after")
    def _citations(self) -> Self:
        if self.assessment not in {"insufficient", "not_applicable"} and not self.sources:
            raise ValueError("assessed model perspective requires a public source citation")
        return self


class USProposalDraft(_Record):
    proposal_id: USProposalId
    author_identity: Text
    prepared_at: _ReportClock
    action: Literal["no_action", "defer"]
    reason: Text
    revisit_when: Text
    # Absent authored rows must not change the digest of an existing reviewed decision.
    model_perspectives: tuple[_USProposalPerspective, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def _unique_perspectives(self) -> Self:
        models = [row.model for row in self.model_perspectives]
        if len(models) != len(set(models)):
            raise ValueError("a US decision may review each model only once")
        return self


class USProposalReview(_Record):
    proposal_id: USProposalId
    draft_sha256: Digest
    reviewer_identity: Text
    reviewed_at: _ReportClock
    conclusion: Literal["pass"]
    notes: Text


def _validate_decision(draft: USProposalDraft, analysis: ReviewedReport) -> None:
    if any(
        source.observed_at > analysis.draft.cutoff_at.astimezone(UTC)
        for row in draft.model_perspectives
        for source in row.sources
    ):
        raise ValueError("US perspective citation was not observed by the report cutoff")
    if draft.prepared_at < analysis.review.reviewed_at:
        raise ValueError("decision must follow the analysis review")
    if draft.action == "no_action" and analysis.draft.comparison is None:
        raise ValueError("no_action requires a comparison based on the exchange close")


def _require_future_horizon(
    draft: USProposalDraft, analysis: ReviewedReport, *, at: datetime
) -> None:
    comparison = analysis.draft.comparison
    if (
        draft.action == "no_action"
        and comparison is not None
        and comparison.horizon_et <= _exchange_date(at)
    ):
        raise ValueError("no_action requires a comparison horizon after the recorded ET date")


def _decision_digest(draft: USProposalDraft, analysis: ReviewedReport) -> str:
    return _digest(
        canonical_json(
            {
                "decision": draft.model_dump(mode="json"),
                "analysis": analysis.model_dump(mode="json"),
            }
        ).encode()
    )


def prepare_us_proposal(
    draft: USProposalDraft, analysis: ReviewedReport, *, sip_proof: SipCloseProof | None = None
) -> str:
    require_close_receipt(analysis.draft, sip_proof)
    _validate_decision(draft, analysis)
    _require_future_horizon(draft, analysis, at=draft.prepared_at)
    render_reviewed_report(analysis)
    return _decision_digest(draft, analysis)


class USProposal(_Record):
    decision: USProposalDraft
    analysis: ReviewedReport
    review: USProposalReview
    published_at: _ReportClock

    @model_validator(mode="after")
    def _review_matches_decision(self) -> Self:
        draft, review = self.decision, self.review
        _validate_decision(draft, self.analysis)
        _bind_report_review(self.analysis.draft, self.analysis.review)
        if (
            draft.proposal_id != review.proposal_id
            or _decision_digest(draft, self.analysis) != review.draft_sha256
            or _one_line(draft.author_identity).casefold()
            == _one_line(review.reviewer_identity).casefold()
            or not draft.prepared_at <= review.reviewed_at <= self.published_at
        ):
            raise ValueError("independent review does not bind the exact US decision")
        _require_future_horizon(draft, self.analysis, at=self.published_at)
        return self


def _published_key(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="microseconds")


def save_us_proposal(
    path: Path,
    draft: USProposalDraft,
    analysis: ReviewedReport,
    review: USProposalReview,
    *,
    published_at: datetime,
    sip_proof: SipCloseProof | None = None,
) -> USProposal:
    # The reviewed analysis is an admission input, not a renderer dependency on App reads.
    require_close_receipt(analysis.draft, sip_proof)
    render_reviewed_report(analysis)
    proposal = USProposal(
        decision=draft, analysis=analysis, review=review, published_at=published_at
    )
    payload = canonical_json(proposal.model_dump(mode="json"))
    with closing(connect_existing(path)) as connection:
        connection.execute(
            "INSERT INTO us_equity_proposal(proposal_id, published_at, payload) VALUES (?, ?, ?)",
            (draft.proposal_id, _published_key(proposal.published_at), payload),
        )
    return proposal


def _load_row(row: sqlite3.Row | None) -> USProposal | None:
    if row is None:
        return None
    proposal = USProposal.model_validate_json(row[2])
    if proposal.decision.proposal_id != row[0] or _published_key(proposal.published_at) != row[1]:
        raise ValueError("stored US decision identity or publication time differs")
    return proposal


def load_us_proposal(connection: sqlite3.Connection, proposal_id: str) -> USProposal | None:
    """Read the exact saved US decision; never look in the Japan proposal tables."""
    return _load_row(
        connection.execute(
            "SELECT proposal_id, published_at, payload FROM us_equity_proposal WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
    )


def published_us_proposal_ids(
    connection: sqlite3.Connection, *, limit: int
) -> tuple[str | None, ...]:
    """List bounded publication slots; a malformed ID is an unnamed unreadable slot."""
    if limit < 1:
        raise ValueError("US publication list needs a positive limit")
    ids: list[str | None] = []
    known: set[str] = set()
    for (raw_id,) in connection.execute(
        "SELECT proposal_id FROM us_equity_proposal "
        "ORDER BY published_at DESC, proposal_id DESC LIMIT ?",
        (limit,),
    ):
        try:
            proposal_id = _US_PROPOSAL_ID.validate_python(raw_id, strict=True)
        except ValidationError:
            ids.append(None)
            continue
        if proposal_id in known:
            raise ValueError("duplicate US publication ID")
        known.add(proposal_id)
        ids.append(proposal_id)
    return tuple(ids)


def load_latest_us_proposal(connection: sqlite3.Connection) -> USProposal | None:
    """Read the latest US decision by UTC publication, without falling back if unreadable."""
    return _load_row(
        connection.execute(
            "SELECT proposal_id, published_at, payload FROM us_equity_proposal "
            "ORDER BY published_at DESC, proposal_id DESC LIMIT 1"
        ).fetchone()
    )
