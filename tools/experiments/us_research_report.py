"""Review, save and read back a private USD research report, not an App proposal."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal, Never, Self
from zoneinfo import ZoneInfo

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    model_validator,
)

from baibai_engine.primitives.json import canonical_json
from baibai_engine.primitives.numbers import finite_decimal

_ET = ZoneInfo("America/New_York")
_EXCHANGE_HOST = {"XNAS": "nasdaq.com", "XNYS": "nyse.com"}

type Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
type ReportId = Annotated[str, Field(pattern=r"^us-report-[a-z0-9-]{1,64}$")]
type Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type USD = Annotated[Decimal, BeforeValidator(finite_decimal), Field(ge=0, max_digits=16)]
type Price = Annotated[USD, Field(gt=0)]


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _IEXBand(_Record):
    feed: Literal["alpaca_iex"]
    basis: Literal["split_adjusted_closes"]
    manifest_sha256: Digest
    received_at: AwareDatetime
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
            or self.received_at.astimezone(_ET).date() <= self.last_session_et
        ):
            raise ValueError("invalid split-adjusted IEX observation window")
        return self


class _OfficialClose(_Record):
    basis: Literal["regular_session_unadjusted"]
    session_et: date
    price_usd: Price
    source_url: HttpUrl
    observed_at: AwareDatetime


class _FinancialEvidence(_Record):
    source_url: HttpUrl
    period_end: date
    observed_at: AwareDatetime
    fact_and_scope: Text


class _Scenario(_Record):
    terminal_value_usd_per_share: USD
    eligible_distributions_usd_per_share: USD
    assumptions: Annotated[tuple[Text, ...], Field(min_length=1)]

    @property
    def total(self) -> Decimal:
        return self.terminal_value_usd_per_share + self.eligible_distributions_usd_per_share


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
    cik: Annotated[str, Field(pattern=r"^[0-9]{10}$")]
    share_class: Text
    currency: Literal["USD"]
    cutoff_at: AwareDatetime
    prepared_at: AwareDatetime
    expected_session_et: date
    session_close_at: AwareDatetime
    calendar_source_url: HttpUrl
    iex_band: _IEXBand | None
    official_close: _OfficialClose | None
    financial_evidence: Annotated[tuple[_FinancialEvidence, ...], Field(min_length=1)]
    thesis: Text
    strongest_countercase: Text
    comparison: _Comparison | None
    unresolved: tuple[Text, ...]

    @model_validator(mode="after")
    def _evidence(self) -> Self:
        cutoff_et = self.cutoff_at.astimezone(_ET).date()
        if (
            self.prepared_at < self.cutoff_at
            or self.session_close_at > self.cutoff_at
            or self.session_close_at.astimezone(_ET).date() != self.expected_session_et
            or not _listing_https(self.calendar_source_url, self.venue)
        ):
            raise ValueError("invalid report cutoff or calendar citation")
        if any(
            fact.observed_at > self.cutoff_at
            or fact.period_end > fact.observed_at.astimezone(_ET).date()
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
        if close is not None and (
            not _listing_https(close.source_url, self.venue)
            or close.session_et != self.expected_session_et
            or close.observed_at > self.cutoff_at
            or close.observed_at < self.session_close_at
        ):
            raise ValueError("official observation requires a matching listing-exchange citation")
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


class ReportReview(_Record):
    report_id: ReportId
    draft_sha256: Digest
    reviewer_identity: Text
    reviewed_at: AwareDatetime
    conclusion: Literal["pass"]
    notes: Text


class _SavedReport(_Record):
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


def render(draft: ReportDraft, review: ReportReview) -> bytes:
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
                f"現金との差 USD {scenario.total - close.price_usd:+f}（各1株当たりの仮定）。"
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


def _unique_fields(fields: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(fields)
    if len(result) != len(fields):
        raise ValueError("duplicate JSON field")
    return result


def _read(path: Path) -> object:
    return json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_unique_fields, parse_float=Decimal
    )


def _save(path: Path, draft: ReportDraft, review: ReportReview) -> None:
    saved = _SavedReport(draft=draft, review=review, rendered_sha256=_digest(render(draft, review)))
    content = canonical_json(saved.model_dump(mode="json")).encode() + b"\n"
    # One create-only private file. Do not replace a draft, prior report or symlink.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(content)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        self.exit(2, "FAIL arguments\n")


def build_parser() -> _Parser:
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("draft-digest").add_argument("draft", type=Path)
    save = commands.add_parser("save")
    save.add_argument("draft", type=Path)
    save.add_argument("review", type=Path)
    save.add_argument("--output", type=Path, required=True)
    commands.add_parser("render").add_argument("saved", type=Path)
    check = commands.add_parser("check-render")
    check.add_argument("saved", type=Path)
    check.add_argument("rendered", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stage = "draft"
    try:
        if args.command in {"draft-digest", "save"}:
            draft = ReportDraft.model_validate(_read(args.draft))
            if args.command == "save":
                stage = "review/save"
                _save(args.output, draft, ReportReview.model_validate(_read(args.review)))
            else:
                print(
                    canonical_json(
                        {"report_id": draft.report_id, "draft_sha256": draft_digest(draft)}
                    )
                )
        else:
            stage = "saved report"
            saved = _SavedReport.model_validate(_read(args.saved))
            content = render(saved.draft, saved.review)
            if _digest(content) != saved.rendered_sha256:
                raise ValueError("saved render digest differs")
            if args.command == "render":
                sys.stdout.buffer.write(content)
            else:
                stage = "rendered file"
                if args.rendered.read_bytes() != content:
                    raise ValueError("rendered content differs")
                print("PASS exact private analytical report render")
    except (OSError, ValueError, TypeError, ArithmeticError):
        print(f"FAIL {stage}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
