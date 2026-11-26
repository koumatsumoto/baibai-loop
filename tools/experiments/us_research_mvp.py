"""Build a private, no-order US research proposal from fixed provider receipts."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from html.parser import HTMLParser
from math import isfinite
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin
from uuid import UUID
from zoneinfo import ZoneInfo

from httpx2 import ByteStream, Headers, MockTransport, Request, Response

from baibai_engine.data_collection.providers.alpaca import (
    BarsResponse,
    DailyBarsQuery,
    capture_daily_bars,
)
from baibai_engine.data_collection.providers.sec_edgar import (
    SecEdgarQuery,
    SecEdgarReceipt,
    SecReportedTicker,
    capture_sec_edgar,
    observe_reported_ticker,
)
from baibai_engine.machine_analysis.models.technical_analysis.calculations import (
    simple_moving_average,
)
from baibai_engine.machine_analysis.observations.financials.sec_companyfacts import (
    SecCompanyFactsCapture,
    SecFilingWitness,
    SecIssuerUsdObservation,
    SecPublicFilingObservation,
    SecReportedDuration,
    SecRevenueAmbiguity,
    observe_sec_comparatives,
    observe_sec_revenue,
)
from baibai_engine.machine_analysis.observations.prices.alpaca_normalization import (
    AlpacaBarSource,
    AlpacaDailyObservation,
    normalize_alpaca_daily_bars,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar

_SCHEMA = "us-research-receipts-v1"
_NY = ZoneInfo("America/New_York")
_CONTENT_HEADERS = {
    "content-type",
    "content-length",
    "content-encoding",
    "content-range",
    "transfer-encoding",
}
_SEC_INDEX_SECTIONS = ("START DOCUMENT DIV", "END DOCUMENT DIV", "START FILER DIV", "END FILER DIV")
_SEC_INDEX_CONTAINERS = {"a", "div", "table"}
_SEC_INDEX_END = re.compile(r"</div>\s*(?:</body>\s*)?(?:</html>)?", re.IGNORECASE)
type _Record = dict[str, Any]
type _Loader = Callable[[str], bytes]


def _clock(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None or not re.search(r"(?:Z|[+-][0-9]{2}:[0-5][0-9])\Z", value):
        raise ValueError("fixed receipt clocks require a Z or ±HH:MM offset")
    # fromisoformat silently truncates; do not collapse a later receipt into the cutoff.
    if any(fraction[6:].strip("0") for fraction in re.findall(r"[.,]([0-9]+)", value)):
        raise ValueError("fixed receipt clocks require exact microsecond precision")
    return stamp.astimezone(UTC)


def _day(value: str) -> date:
    day = date.fromisoformat(value)
    if day.isoformat() != value:
        raise ValueError("fixed periods require ISO dates")
    return day


def _native(row: _Record, load: _Loader) -> tuple[bytes, int, datetime]:
    body = load(row["file"])
    if sha256(body).hexdigest() != row["sha256"]:
        raise ValueError("raw body SHA-256 mismatch")
    status = row["status"]
    if type(status) is not int or not 100 <= status <= 599:
        raise ValueError("invalid captured HTTP status")
    return body, status, _clock(row["received_at"])


def _content_headers(record: _Record) -> Headers:
    raw = record.get("headers", {})
    if not isinstance(raw, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in raw.items()
    ):
        raise ValueError("invalid captured response headers")
    return Headers({key: value for key, value in raw.items() if key.lower() in _CONTENT_HEADERS})


def _whole_identity_headers(record: _Record, body: bytes) -> Headers:
    """Check recorded wire claims about these fixed, unencoded original bytes."""
    headers = _content_headers(record)
    length = headers.get("Content-Length")
    transfer = headers.get("Transfer-Encoding")
    if (
        "Content-Range" in headers
        or headers.get("Content-Encoding", "identity").strip().lower() != "identity"
        or (transfer is not None and (transfer.strip().lower() != "chunked" or length is not None))
        or (length is not None and (length.strip().lstrip("0") or "0") != str(len(body)))
    ):
        raise ValueError("incomplete, encoded or inconsistent original response")
    return headers


def _recorded_sec(
    query: SecEdgarQuery, record: _Record, load: _Loader
) -> SecEdgarReceipt | Literal["transport_error"]:
    attempt_id = record["attempt_id"]
    if not isinstance(attempt_id, str):
        raise ValueError("a fixed SEC attempt ID must be a UUID string")
    body, status, received = _native(record, load)
    headers = _content_headers(record)

    def recorded_response(request: Request) -> Response:
        return Response(status, stream=ByteStream(body), headers=headers, request=request)

    return capture_sec_edgar(
        query,
        transport=MockTransport(recorded_response),
        contact_email="offline-replay@example.invalid",
        attempt_id=UUID(attempt_id),
        clock=lambda: received,
    )


def _complete_single_hop(record: _Record, url: str) -> bool:
    return (
        record.get("request_url") == record.get("response_url") == url
        and record.get("complete") is True
        and record.get("redirected") is False
    )


def _ticker_identity(
    subject: _Record, symbol: str, cik: str, load: _Loader, cutoff: datetime
) -> SecReportedTicker | str:
    record = subject.get("submissions")
    if record is None:
        return "sec_identity:submissions_receipt_missing"
    try:
        if _clock(record["received_at"]) > cutoff:
            return "sec_identity:not_yet_received"
        query = SecEdgarQuery("submissions", cik)
        if not _complete_single_hop(record, query.url):
            return "sec_identity:submissions_response_scope_unverified"
        receipt = _recorded_sec(query, record, load)
        if isinstance(receipt, str):
            return f"sec_identity:{receipt}"
        observed = observe_reported_ticker(receipt, symbol=symbol, expected_cik=cik, cutoff=cutoff)
        return f"sec_identity:{observed}" if isinstance(observed, str) else observed
    except (KeyError, TypeError, ValueError, OSError, ArithmeticError):
        return "sec_identity:invalid_fixed_submissions"


def _alpaca(
    spec: _Record, symbols: tuple[str, ...], load: _Loader, cutoff: datetime
) -> tuple[tuple[AlpacaDailyObservation, ...], str | None]:
    try:
        pages = spec["pages"]
        if not isinstance(pages, list) or not pages:
            return (), "alpaca_receipt_missing"
        # Even an empty terminal page defines completeness. Do not read future bodies.
        if any(_clock(page["received_at"]) > cutoff for page in pages):
            return (), "alpaca_chain_not_yet_received"
        query = DailyBarsQuery(symbols, _day(spec["start"]), _day(spec["end"]), "-", "split")
        receipts = iter(pages)

        def recorded_response(url: str) -> BarsResponse:
            page = next(receipts, None)
            if page is None:
                raise OSError("no fixed receipt for next page")
            if not _complete_single_hop(page, url):
                raise ValueError("Alpaca page was not fully received from the fixed query")
            body, status, received = _native(page, load)
            if status == 200:
                _whole_identity_headers(page, body)
            return BarsResponse(status, received, body)

        capture = capture_daily_bars(
            query, request_id=spec["request_id"], transport=recorded_response
        )
        if capture.failure is not None:
            return (), f"alpaca_incomplete:{capture.failure}"
        if next(receipts, None) is not None:
            return (), "alpaca_invalid_fixed_receipt"
        return normalize_alpaca_daily_bars(capture), None
    except (KeyError, TypeError, ValueError, OSError, ArithmeticError):
        return (), "alpaca_invalid_fixed_receipt"


def _filing(record: _Record) -> SecFilingWitness:
    form = record["form"]
    if form not in ("10-Q", "10-Q/A", "10-K"):
        raise ValueError("requires an independently pinned supported periodic filing")
    published = record["published_at"]
    return SecFilingWitness(
        issuer_cik=record["issuer_cik"],
        accession=record["accession"],
        form=form,
        receipt_id=record["receipt_id"],
        accepted_at=_clock(record["accepted_at"]),
        received_at=_clock(record["received_at"]),
        published_at=None if published is None else _clock(published),
        publication_ref=record["publication_ref"],
    )


class _IndexDocument(HTMLParser):
    """Recognize the SEC index title and the complete submission link in its body."""

    def __init__(self, index_url: str) -> None:
        super().__init__()
        self._index_url = index_url
        self._submission_url = index_url.removesuffix("-index.htm") + ".txt"
        self.titles: list[str] = []
        self.complete = False
        self.is_native = False
        self._in_head = False
        self._in_body = False
        self._body_closed = False
        self._text: list[str] | None = None
        self._link: bool | None = None
        self._closed_link = False
        self._nested_link = False
        self._containers: list[str] = []
        self._sections: list[tuple[str, tuple[int, int]]] = []
        self._native_link = False
        self._in_filer = False
        self._native_filer = False
        self._native_closed = False
        self._native_invalid = False
        self._form_group_depth = 0
        self._form_field: tuple[Literal["heading", "value"], list[str]] | None = None
        self._expect_report_end = False
        self._report_ends: list[str | None] = []

    def report_end(self) -> date:
        if len(self._report_ends) != 1 or (value := self._report_ends[0]) is None:
            raise ValueError("SEC filing index needs one Period of Report")
        return _day(value)

    def _open_form_field(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        expect_end = self._expect_report_end
        self._expect_report_end = False
        self._form_field = None
        # The filing details precede document and filer sections.
        if tag != "div" or not self._in_body or self._sections:
            return
        if not self._form_group_depth:
            self._form_group_depth = int(("class", "formGrouping") in attrs)
            return
        if self._form_group_depth == 1:
            if expect_end and ("class", "info") in attrs:
                self._form_field = ("value", [])
            elif ("class", "infoHead") in attrs:
                self._form_field = ("heading", [])
        self._form_group_depth += 1

    def _close_form_field(self, tag: str) -> None:
        field = self._form_field
        self._form_field = None
        self._expect_report_end = False
        if tag != "div" or not self._form_group_depth:
            return
        self._form_group_depth -= 1
        if self._form_group_depth == 1 and field is not None:
            role, fragments = field
            value = "".join(fragments).strip()
            if role == "value":
                self._report_ends[-1] = value
            elif value == "Period of Report":
                self._report_ends.append(None)
                self._expect_report_end = True

    @property
    def has_submission(self) -> bool:
        return self._closed_link and self._link is None and not self._nested_link

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "head":
            self._in_head = True
        elif tag == "title" and self._in_head:
            self._text = []
        elif tag == "body":
            self._native_invalid |= self._in_body or self._body_closed
            self._in_body = True
        elif tag == "a" and self._in_body:
            self._nested_link |= self._link is not None
            hrefs = [value for key, value in attrs if key == "href"]
            self._link = (
                len(hrefs) == 1
                and (href := hrefs[0]) is not None
                and urljoin(self._index_url, href) == self._submission_url
            )
        if tag == "div" and ("id", "contentDiv") in attrs:
            self.is_native = True
            self._native_invalid |= (
                not self._in_body or bool(self._containers) or self._native_closed
            )
            self._containers.append(tag)
        elif self._containers and tag in _SEC_INDEX_CONTAINERS:
            if len(self._sections) == 3 and self._containers == ["div"] and tag == "div":
                self._in_filer = ("class", "filerDiv") in attrs
            self._containers.append(tag)
        self._open_form_field(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _SEC_INDEX_CONTAINERS:
            super().handle_startendtag(tag, attrs)
        elif tag == "div" and ("id", "contentDiv") in attrs:
            self.is_native = self._native_invalid = True
        elif self._containers:
            self._native_invalid = True
        self._form_field = None
        self._expect_report_end = False

    def handle_endtag(self, tag: str) -> None:
        self._close_form_field(tag)
        if tag == "head":
            self._in_head = False
            self._text = None
        elif tag == "title" and self._text is not None:
            self.titles.append(" ".join("".join(self._text).split()))
            self._text = None
        elif tag == "a" and self._link is not None:
            if self._link:
                self._closed_link = True
                self._native_link |= len(self._sections) == 1 and "table" in self._containers
            self._link = None
        elif tag == "body":
            self._native_invalid |= not self._in_body or bool(self._containers)
            if self._in_body:
                self._in_body = False
                self._body_closed = True
        elif tag == "html":
            self._native_invalid |= not self._native_closed
            if self._body_closed:
                self.complete = True
        if self._containers and tag in _SEC_INDEX_CONTAINERS:
            if tag != self._containers[-1]:
                self._native_invalid = True
            else:
                self._containers.pop()
                self._native_closed |= not self._containers
                if self._containers == ["div"]:
                    self._in_filer = False

    def handle_comment(self, data: str) -> None:
        name = data.strip()
        if name not in _SEC_INDEX_SECTIONS:
            return
        self.is_native = True
        if not self._containers or (name != _SEC_INDEX_SECTIONS[0] and self._containers != ["div"]):
            self._native_invalid = True
        self._sections.append((name, self.getpos()))

    def handle_data(self, data: str) -> None:
        if self._form_field is not None:
            self._form_field[1].append(data)
        if data.strip():
            self._expect_report_end = False
        if self._text is not None:
            self._text.append(data)
        elif self._in_filer and self.cdata_elem is None and any(c.isalnum() for c in data):
            self._native_filer = True

    def native_end(self, text: str) -> bool:
        if (
            self._native_invalid
            or not self._native_closed
            or not self._native_link
            or not self._native_filer
            or tuple(name for name, _ in self._sections) != _SEC_INDEX_SECTIONS
        ):
            return False
        lines = text.split("\n")
        # HTMLParser can also emit a comment event for a bogus declaration. Require
        # the template's literal comments at the parser's actual source positions.
        for name, (line, column) in self._sections:
            if not lines[line - 1][column:].startswith(f"<!-- {name} -->"):
                return False
        name, (line, column) = self._sections[-1]
        tail = "\n".join(lines[line - 1 :])[column:]
        return _SEC_INDEX_END.fullmatch(tail.removeprefix(f"<!-- {name} -->").strip()) is not None


def _public_index(
    record: _Record | None, load: _Loader, filing: SecFilingWitness, cutoff: datetime
) -> SecPublicFilingObservation | None:
    # A future receipt must not contribute evidence or invalidate an earlier proof.
    if record is None or _clock(record["received_at"]) > cutoff:
        return None
    url = record["request_url"]
    if not isinstance(url, str) or not _complete_single_hop(record, url):
        raise ValueError("SEC filing index requires a complete, single-hop response")
    body, status, observed_at = _native(record, load)
    if status != 200:
        raise ValueError("SEC filing index was not received with HTTP 200")
    headers = _whole_identity_headers(record, body)
    length = headers.get("Content-Length")
    media = headers.get("Content-Type", "text/html").split(";", 1)[0].strip().lower()
    if media not in {"text/html", "application/xhtml+xml"}:
        raise ValueError("unsupported SEC filing index content type")
    text = body.decode("utf-8", errors="replace")
    parser = _IndexDocument(url)
    parser.feed(text)
    parser.close()
    complete = parser.complete
    if parser.is_native:
        # Explicit outer tags cannot substitute for any part of recognized EDGAR content.
        optional_end = media == "text/html" and "Content-Type" in headers and length is not None
        complete = (complete or optional_end) and parser.native_end(text)
    if not (
        complete
        and parser.has_submission
        and parser.titles == [f"EDGAR Filing Documents for {filing.accession}"]
    ):
        raise ValueError("SEC filing index body is incomplete or identifies another accession")
    identity = json.dumps([url, observed_at.isoformat(), record["sha256"]]).encode()
    return SecPublicFilingObservation(
        issuer_cik=filing.issuer_cik,
        accession=filing.accession,
        observed_at=observed_at,
        receipt_id=f"sec-index:{sha256(identity).hexdigest()}",
        index_url=url,
        report_end=parser.report_end(),
    )


def _sec_proof(filing: SecFilingWitness, observed: SecPublicFilingObservation | None) -> str:
    if filing.publication_ref is not None:
        return f"独立公表証拠 `{_label(filing.publication_ref)}`"
    if observed is not None:
        return (
            f"SEC提出索引の公開確認: {observed.observed_at.isoformat()}"
            "（初回公表の証明ではない）; "
            f"`{observed.receipt_id}`; {observed.index_url}"
        )
    raise ValueError("SEC observation has no publication evidence")


def _sec(
    subject: _Record,
    load: _Loader,
    cutoff: datetime,
    *,
    compare: tuple[str, SecReportedDuration, date, date] | None = None,
) -> tuple[list[str], str | None]:
    source = subject.get("companyfacts")
    if source is None:
        return [], "sec_companyfacts_receipt_missing"
    evidence: list[str] = []
    try:
        if _clock(source["received_at"]) > cutoff:
            return [], "sec_companyfacts_not_yet_received"
        query = SecEdgarQuery("companyfacts", subject["cik"])
        if not _complete_single_hop(source, query.url):
            return [], "sec_companyfacts_response_scope_unverified"
        wire = _recorded_sec(query, source, load)
        if isinstance(wire, str):
            return [], "sec_transport_error"
        evidence.append(
            f"SEC CompanyFacts: {wire.state}; {wire.received_at.isoformat()}; "
            f"SHA `{wire.body_sha256}`"
        )
        if wire.state != "received":
            return evidence, f"sec_companyfacts:{wire.state}"

        filing_record = subject.get("filing")
        if filing_record is None:
            return evidence, "sec_filing_witness_missing"
        if _clock(filing_record["received_at"]) > cutoff:
            return evidence, "sec_filing_not_yet_received"
        public_index = filing_record.get("public_index")
        public_seen = filing_record.get("public_seen_at") if public_index is None else None
        if public_seen is not None and (seen := _clock(public_seen)) <= cutoff:
            if seen < _clock(filing_record["accepted_at"]):
                return evidence, "sec_invalid_fixed_evidence"
            evidence.append(
                f"公開を見た申告時刻: {seen.isoformat()}（原本未照合・初回公表の証明ではない）"
            )
        if filing_record["published_at"] is None:
            evidence.append("独立した初回公表時刻は未確認。")
        form = filing_record["form"]
        if form == "10-K/A" or (compare is not None and form == "10-K"):
            return evidence, "sec_filing_form_not_covered"
        filing = _filing(filing_record)
        period = SecReportedDuration(_day(subject["period_start"]), _day(subject["period_end"]))
        is_annual = filing.form == "10-K"
        if is_annual and not period.fits_regular_fiscal_year:
            return evidence, "sec_period_outside_supported_fiscal_year"
        if not is_annual and not period.fits_regular_quarter:
            return evidence, "sec_period_outside_supported_quarter"
        capture = SecCompanyFactsCapture(
            wire.receipt_id, wire.received_at, wire.body_sha256, wire.body
        )
        observed = _public_index(public_index, load, filing, cutoff)
        if compare is not None:
            reporting_quarter, prior, current_fiscal_start, prior_fiscal_start = compare
            comparison = observe_sec_comparatives(
                capture,
                filing,
                expected_cik=query.cik,
                reporting_quarter=reporting_quarter,
                cutoff=cutoff,
                publicly_observed_by=observed,
                current_quarter=period,
                prior_quarter=prior,
                current_fiscal_start=current_fiscal_start,
                prior_fiscal_start=prior_fiscal_start,
            )
            if isinstance(comparison, str):
                return evidence, f"sec_{comparison}"
            evidence.append(
                f"CIK `{query.cik}`; 提出 `{filing.accession}` ({filing.form}); "
                f"指定会計四半期 `{reporting_quarter}`; {_sec_proof(filing, observed)}"
            )
            for row in comparison:
                for column, period, fact in (
                    ("current", row.current_period, row.current),
                    ("prior", row.prior_period, row.prior),
                ):
                    if isinstance(fact, SecIssuerUsdObservation):
                        value = (
                            f"USD {fact.amount:,}; `{fact.concept}`; `{', '.join(fact.fact_paths)}`"
                        )
                    else:
                        reason = fact.reason if isinstance(fact, SecRevenueAmbiguity) else fact
                        value = f"未採用: `{reason}`"
                    evidence.append(
                        f"- {row.metric} ({row.basis}, {column}) "
                        f"{period.start}〜{period.end}: {value}"
                    )
            return evidence, None
        result = observe_sec_revenue(
            capture,
            filing,
            expected_cik=subject["cik"],
            period_start=period.start,
            period_end=period.end,
            cutoff=cutoff,
            publicly_observed_by=observed,
        )
        if isinstance(result, str):
            return evidence, f"sec_{result}"
        if isinstance(result, SecRevenueAmbiguity):
            for candidate in result.candidates:
                evidence.append(
                    f"未採用のSECネイティブ概念 `{candidate.concept}`; "
                    f"原本位置 `{', '.join(candidate.fact_paths)}`"
                )
            return evidence, f"sec_revenue_concepts_unresolved:{result.reason}"
        proof = _sec_proof(result.filing, result.publicly_observed_by)
        if is_annual:
            evidence.append("通期は指定した期間。より新しい提出の有無は確認していない。")
        revenue_label = "SEC通期実績売上" if is_annual else "SEC実績売上"
        evidence += [
            (
                f"{revenue_label}: USD {result.amount:,} "
                f"({result.period_start}〜{result.period_end}, `{result.concept}`)"
            ),
            (
                f"提出 `{result.filing.accession}` ({result.filing.form}); "
                f"{proof}; 原本位置 `{', '.join(result.fact_paths)}`"
            ),
        ]
        return evidence, None
    except (KeyError, TypeError, ValueError, OSError, ArithmeticError):
        return evidence, "sec_invalid_fixed_evidence"


def _observed_prices(
    symbol: str,
    observations: tuple[AlpacaDailyObservation, ...],
) -> tuple[list[tuple[AlpacaDailyObservation, AlpacaBarSource]], list[date]]:
    rows = sorted(
        (bar for bar in observations if bar.requested_symbol == symbol),
        key=lambda bar: bar.trade_date,
    )
    eligible = []
    provisional = []
    for row in rows:
        later = next(
            (
                source
                for source in row.sources
                if source.received_at.astimezone(_NY).date() > row.trade_date
            ),
            None,
        )
        if later is None:
            provisional.append(row.trade_date)
        else:
            eligible.append((row, later))
    return eligible, provisional


def _price(
    symbol: str,
    observations: tuple[AlpacaDailyObservation, ...],
    problem: str | None,
) -> tuple[list[str], str | None]:
    if problem is not None:
        return [], problem
    eligible, provisional = _observed_prices(symbol, observations)
    if not eligible and not provisional:
        return [], "alpaca_no_observed_bar"
    evidence: list[str] = []
    if provisional:
        evidence.append(
            f"NY当日受領のみの観測 {len(provisional)}件（最終 {provisional[-1]}）は途中集計扱い。"
            "候補判定とSMAには含めない。"
        )
    if not eligible:
        return evidence, "alpaca_prior_observed_bar_missing"
    latest, source = eligible[-1]
    evidence += [
        (
            f"IEX分割調整済み日次集計の最新観測: {latest.trade_date} USD {latest.close}/株"
            "（途中集計の可能性があり、公式引け・発注価格ではない）"
        ),
        (
            f"同観測を含む受領: {source.received_at.isoformat()}; "
            f"SHA `{source.body_sha256}`; 行 `{source.json_pointer}`"
        ),
    ]
    if len(eligible) < 20:
        return evidence, "alpaca_fewer_than_20_observed_closes"
    series = [PriceBar(row.trade_date, float(row.close), None) for row, _ in eligible[-20:]]
    if any(not isfinite(bar.price) or bar.price <= 0 for bar in series):
        return evidence, "alpaca_selected_window_out_of_calculation_range"
    try:
        sma = simple_moving_average(series, window_bars=20)
    except (ValueError, OverflowError):
        return evidence, "alpaca_observed_sma_unavailable"
    if (
        sma["status"] != "available"
        or not isinstance(sma["value"], float)
        or not isinstance(sma["gap_ratio"], float)
    ):
        return evidence, "alpaca_observed_sma_unavailable"
    evidence.append(
        f"観測20本のSMA: USD {sma['value']:.2f}（{series[0].traded_at}〜{latest.trade_date}）、"
        f"最新値の乖離 {sma['gap_ratio']:+.2%}（取引暦の充足は未確認）"
    )
    return evidence, None


def _label(value: object) -> str:
    # References can be human-provided; leave them plain and on a single Markdown line.
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing fixed evidence reference")
    return " ".join(value.split()).replace("`", "'")


def _research_scope(bundle: _Record) -> tuple[list[_Record], tuple[str, ...], datetime]:
    if bundle["schema"] != _SCHEMA:
        raise ValueError("unknown fixed receipt schema")
    subjects = bundle["subjects"]
    if not isinstance(subjects, list) or not 1 <= len(subjects) <= 3:
        raise ValueError("choose one to three fixed research subjects")
    symbols = tuple(subject["symbol"] for subject in subjects)
    cutoff = _clock(bundle["cutoff"])
    # Validate the native labels even if there is no usable page.
    DailyBarsQuery(symbols, cutoff.date(), cutoff.date(), "-", "split")
    return subjects, symbols, cutoff


def _accept_iex_session(bundle: _Record, load: _Loader, *, expected: date) -> tuple[str, bool]:
    _, symbols, cutoff = _research_scope(bundle)
    observations, problem = _alpaca(bundle["alpaca"], symbols, load, cutoff)
    lines = [
        "# 米国株 IEX取引日確認（発注なし）",
        f"期待NY取引日: {expected}",
        f"評価cutoff: {cutoff.isoformat()}",
        "IEX観測の日付だけを照合する。公式引け・売買判断は確認しない。",
    ]
    if problem is not None:
        lines.append(f"- 受領不備: `{problem}`")
        return "\n".join(lines) + "\n", False
    all_matched = True
    for symbol in symbols:
        eligible, provisional = _observed_prices(symbol, observations)
        latest = eligible[-1][0].trade_date if eligible else None
        matched = latest == expected
        all_matched = all_matched and matched
        status = "一致" if matched else "不一致"
        lines.append(f"- {symbol}: {status}（採用最終日: {latest or 'なし'}）")
        if expected in provisional:
            lines.append(f"  - {expected}はNY当日受領のみのため途中集計扱い。")
    return "\n".join(lines) + "\n", all_matched


def render_research(bundle: _Record, load: _Loader, *, synthetic: bool) -> str:
    """Compose actual provider adapters without executing a provider request or writing state."""
    subjects, symbols, cutoff = _research_scope(bundle)
    observations, problem = _alpaca(bundle["alpaca"], symbols, load, cutoff)
    origin = (
        "合成fixture。実データ・売買判断ではない。"
        if synthetic
        else "指定された固定受領と銘柄対応によるリサーチ。元の取得・公開事実を再認証しない。"
    )
    lines = ["# 米国株 リサーチ候補（発注なし）", "", origin, f"評価cutoff: {cutoff.isoformat()}"]
    for subject in subjects:
        symbol = subject["symbol"]
        cik = SecEdgarQuery("companyfacts", subject["cik"]).cik
        price_notes, price_problem = _price(symbol, observations, problem)
        identity = _ticker_identity(subject, symbol, cik, load, cutoff)
        sec_notes, sec_problem = (
            ([], identity) if isinstance(identity, str) else _sec(subject, load, cutoff)
        )
        reasons = [reason for reason in (price_problem, sec_problem) if reason is not None]
        state = "hold_for_evidence" if reasons else "research_candidate"
        lines.extend(["", f"## {symbol} | {state}", f"- 照会した発行体 CIK `{cik}`。"])
        if not isinstance(identity, str):
            lines.append(
                f"- SEC提出一覧の銘柄対応: `{identity.identity_ref}` "
                f"（受領 {identity.received_at.isoformat()}）。市場・株式クラスは表紙で別確認。"
            )
        lines.extend(f"- {note}" for note in price_notes + sec_notes)
        if reasons:
            lines.append(f"- 未充足: `{', '.join(reasons)}`")
            lines.append("- 次の調査: 不足した受領・公開根拠・観測窓を確認してから比較する。")
        else:
            lines.append(
                "- 次の調査: 売上が株主に帰属するキャッシュへどうつながるかを比較決算で検証する。"
            )
        lines.append(
            "- 売買: `no_action`。この集計と売上だけでは、"
            "一株価値・保有資金・執行条件を判断できない。"
        )
    return "\n".join(lines) + "\n"


def _fixture() -> tuple[_Record, dict[str, bytes]]:
    files: dict[str, bytes] = {}
    at = "2026-11-21T06:00:00Z"
    start = date(2026, 10, 22)
    days = [
        start + timedelta(days=i) for i in range(30) if (start + timedelta(days=i)).weekday() < 5
    ]
    bars = [
        {
            "t": datetime(day.year, day.month, day.day, tzinfo=_NY).astimezone(UTC).isoformat(),
            "o": 90 + i,
            "h": 92 + i,
            "l": 89 + i,
            "c": 91 + i,
            "v": 700 + i,
        }
        for i, day in enumerate(days)
    ]

    def add(name: str, body: object, **meta: object) -> _Record:
        raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        files[name] = raw
        return {
            "file": name,
            "sha256": sha256(raw).hexdigest(),
            "status": 200,
            "received_at": at,
            **meta,
        }

    def submissions(symbol: str, cik: str, attempt: int) -> _Record:
        url = SecEdgarQuery("submissions", cik).url
        return add(
            f"{symbol}-submissions.json",
            {
                "cik": cik,
                "tickers": [symbol],
                "filings": {
                    "recent": {"accessionNumber": [], "form": [], "acceptanceDateTime": []},
                    "files": [],
                },
            },
            request_url=url,
            response_url=url,
            attempt_id=str(UUID(int=attempt)),
            complete=True,
            redirected=False,
        )

    query = DailyBarsQuery(("AAPL", "NVDA"), start, days[-1], "-", "split")
    page = add(
        "alpaca.json",
        # The native terminal-page cursor is JSON null, not a credential.
        {"bars": {"AAPL": bars, "NVDA": bars[-1:]}, "next_page_token": None},  # nosec B105
        request_url=query.url(),
        response_url=query.url(),
        complete=True,
        redirected=False,
    )
    accession = "0000000000-26-000001"
    fact = {
        "accn": accession,
        "form": "10-Q",
        "start": "2026-07-01",
        "end": "2026-09-30",
        "val": 1234567890,
    }
    companyfacts_url = SecEdgarQuery("companyfacts", "0000320193").url
    companyfacts = add(
        "apple.json",
        {
            "cik": 320193,
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {"USD": [fact]}
                    }
                }
            },
        },
        attempt_id="00000000-0000-0000-0000-000000000001",
        request_url=companyfacts_url,
        response_url=companyfacts_url,
        complete=True,
        redirected=False,
    )
    period = {"period_start": fact["start"], "period_end": fact["end"]}
    return {
        "schema": _SCHEMA,
        "cutoff": "2026-11-21T06:00:00Z",
        "alpaca": {
            "request_id": "fixture-us-mvp",
            "start": start.isoformat(),
            "end": days[-1].isoformat(),
            "pages": [page],
        },
        "subjects": [
            {
                "symbol": "AAPL",
                "cik": "0000320193",
                "submissions": submissions("AAPL", "320193", 2),
                **period,
                "companyfacts": companyfacts,
                "filing": {
                    "issuer_cik": "0000320193",
                    "accession": accession,
                    "form": "10-Q",
                    "receipt_id": "fixture-filing",
                    "accepted_at": "2026-11-04T20:45:00Z",
                    "published_at": "2026-11-04T21:00:00Z",
                    "publication_ref": "fixture-publication",
                    "received_at": "2026-11-04T21:02:00Z",
                },
            },
            {
                "symbol": "NVDA",
                "cik": "0001045810",
                "submissions": submissions("NVDA", "1045810", 3),
                **period,
                "companyfacts": None,
                "filing": None,
            },
        ],
    }, files


def _compare_sec_report(bundle: _Record, load: _Loader, args: Sequence[str]) -> str:
    if bundle["schema"] != _SCHEMA:
        raise ValueError("unknown fixed receipt schema")
    cik, quarter, prior_start, prior_end, current_fiscal_start, prior_fiscal_start = args
    issuer = SecEdgarQuery("companyfacts", cik).cik
    fields = ("cik", "period_start", "period_end", "companyfacts", "filing")
    fixed = [
        {field: subject.get(field) for field in fields}
        for subject in bundle["subjects"]
        if subject["cik"] == issuer
    ]
    if not fixed or any(entry != fixed[0] for entry in fixed[1:]):
        raise ValueError("one fixed filing per SEC comparison")
    prior = SecReportedDuration(_day(prior_start), _day(prior_end))
    notes, problem = _sec(
        fixed[0],
        load,
        _clock(bundle["cutoff"]),
        compare=(quarter, prior, _day(current_fiscal_start), _day(prior_fiscal_start)),
    )
    if problem is not None:
        notes.append(f"- 未採用: `{problem}`")
    return "# SEC同一提出の発行体財務比較（株価・売買判定なし）\n\n" + "\n".join(notes) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle", type=Path, help="fixed JSON manifest and adjacent original response byte files"
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--compare-sec",
        nargs=6,
        metavar=(
            "CIK",
            "Q1_OR_Q2_OR_Q3",
            "PRIOR_Q_START",
            "PRIOR_Q_END",
            "CURRENT_FY_START",
            "PRIOR_FY_START",
        ),
        help="report only fixed-issuer SEC comparative facts from --bundle",
    )
    modes.add_argument(
        "--accept-iex-session",
        type=_day,
        metavar="YYYY-MM-DD",
        help="check each subject's latest eligible IEX date against the expected NY session",
    )
    args = parser.parse_args(argv)
    manifest: Path | None = args.bundle
    if args.compare_sec is not None and manifest is None:
        parser.error("--compare-sec requires --bundle")
    if args.accept_iex_session is not None and manifest is None:
        parser.error("--accept-iex-session requires --bundle")
    exit_code = 0
    try:
        if manifest is None:
            bundle, fixtures = _fixture()
            report = render_research(bundle, fixtures.__getitem__, synthetic=True)
        else:
            root = manifest.resolve().parent

            def read_native(name: str) -> bytes:
                if Path(name).name != name or name in ("", ".", ".."):
                    raise ValueError("provider body file must be adjacent to the manifest")
                path = (root / name).resolve()
                if path.parent != root:
                    raise ValueError("provider body file escapes the designated directory")
                return path.read_bytes()

            bundle = json.loads(manifest.read_text(encoding="utf-8"))
            if args.accept_iex_session is not None:
                report, matched = _accept_iex_session(
                    bundle, read_native, expected=args.accept_iex_session
                )
                exit_code = 0 if matched else 1
            elif args.compare_sec is not None:
                report = _compare_sec_report(bundle, read_native, args.compare_sec)
            else:
                report = render_research(bundle, read_native, synthetic=False)
    except (KeyError, TypeError, ValueError, OSError, ArithmeticError):
        parser.error("invalid fixed US research receipt bundle")
    print(report, end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
