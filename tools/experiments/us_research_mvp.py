"""Build a private, no-order US research proposal from fixed provider receipts."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from math import isfinite
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from httpx2 import ByteStream, MockTransport, Request, Response

from baibai_engine.data_collection.providers.alpaca import (
    BarsResponse,
    DailyBarsQuery,
    capture_daily_bars,
)
from baibai_engine.data_collection.providers.sec_edgar import SecEdgarQuery, capture_sec_edgar
from baibai_engine.machine_analysis.models.technical_analysis.calculations import (
    simple_moving_average,
)
from baibai_engine.machine_analysis.observations.financials.sec_companyfacts import (
    SecCompanyFactsCapture,
    SecFilingWitness,
    SecRevenueAmbiguity,
    observe_sec_revenue,
)
from baibai_engine.machine_analysis.observations.prices.alpaca_normalization import (
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
type _Record = dict[str, Any]
type _Loader = Callable[[str], bytes]


def _clock(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError("fixed receipt clocks require an offset")
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
            if page["request_url"] != url:
                raise ValueError("Alpaca page request does not match the fixed query")
            body, status, received = _native(page, load)
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
    if form not in ("10-Q", "10-Q/A"):
        raise ValueError("requires independently pinned quarterly filing")
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


def _sec(subject: _Record, load: _Loader, cutoff: datetime) -> tuple[list[str], str | None]:
    source = subject.get("companyfacts")
    if source is None:
        return [], "sec_companyfacts_receipt_missing"
    evidence: list[str] = []
    try:
        if _clock(source["received_at"]) > cutoff:
            return [], "sec_companyfacts_not_yet_received"
        body, status, received = _native(source, load)
        headers = {
            key: value
            for key, value in source.get("headers", {}).items()
            if key.lower() in _CONTENT_HEADERS
        }

        def recorded_response(request: Request) -> Response:
            return Response(status, stream=ByteStream(body), headers=headers, request=request)

        query = SecEdgarQuery("companyfacts", subject["cik"])
        if source.get("request_url", query.url) != query.url:
            return [], "sec_request_scope_mismatch"
        wire = capture_sec_edgar(
            query,
            transport=MockTransport(recorded_response),
            contact_email="offline-replay@example.invalid",
            attempt_id=UUID(source["attempt_id"]),
            clock=lambda: received,
        )
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
        public_seen = filing_record.get("public_seen_at")
        if public_seen is not None and (seen := _clock(public_seen)) <= cutoff:
            if seen < _clock(filing_record["accepted_at"]):
                return evidence, "sec_invalid_fixed_evidence"
            evidence.append(
                f"提出の公開を確認した時刻: {seen.isoformat()}（初回公表の証明ではない）"
            )
        if filing_record["published_at"] is None:
            evidence.append("独立した初回公表時刻は未確認。")
        if filing_record["form"] in ("10-K", "10-K/A"):
            return evidence, "sec_filing_form_not_covered"
        filing = _filing(filing_record)
        result = observe_sec_revenue(
            SecCompanyFactsCapture(wire.receipt_id, wire.received_at, wire.body_sha256, wire.body),
            filing,
            expected_cik=subject["cik"],
            period_start=_day(subject["period_start"]),
            period_end=_day(subject["period_end"]),
            cutoff=cutoff,
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
        publication_ref = _label(result.filing.publication_ref)
        evidence += [
            (
                f"SEC実績売上: USD {result.amount:,} "
                f"({result.period_start}〜{result.period_end}, `{result.concept}`)"
            ),
            (
                f"提出 `{result.filing.accession}` ({result.filing.form}); "
                f"独立公表証拠 `{publication_ref}`; 原本位置 `{', '.join(result.fact_paths)}`"
            ),
        ]
        return evidence, None
    except (KeyError, TypeError, ValueError, OSError, ArithmeticError):
        return evidence, "sec_invalid_fixed_evidence"


def _price(
    symbol: str,
    observations: tuple[AlpacaDailyObservation, ...],
    problem: str | None,
) -> tuple[list[str], str | None]:
    if problem is not None:
        return [], problem
    rows = sorted(
        (bar for bar in observations if bar.requested_symbol == symbol),
        key=lambda bar: bar.trade_date,
    )
    if not rows:
        return [], "alpaca_no_observed_bar"
    evidence: list[str] = []
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


def render_research(bundle: _Record, load: _Loader, *, synthetic: bool) -> str:
    """Compose actual provider adapters without executing a provider request or writing state."""
    if bundle["schema"] != _SCHEMA:
        raise ValueError("unknown fixed receipt schema")
    subjects = bundle["subjects"]
    if not isinstance(subjects, list) or not 1 <= len(subjects) <= 3:
        raise ValueError("choose one to three fixed research subjects")
    symbols = tuple(subject["symbol"] for subject in subjects)
    if len(set(symbols)) != len(symbols):
        raise ValueError("one fixed issuer identity per symbol")
    cutoff = _clock(bundle["cutoff"])
    observations, problem = _alpaca(bundle["alpaca"], symbols, load, cutoff)
    origin = (
        "合成fixture。実データ・売買判断ではない。"
        if synthetic
        else "指定された固定受領と銘柄対応によるリサーチ。元の取得・公開事実を再認証しない。"
    )
    lines = ["# 米国株 リサーチ候補（発注なし）", "", origin, f"評価cutoff: {cutoff.isoformat()}"]
    for subject in subjects:
        symbol = subject["symbol"]
        # Validate the native label even if there is no usable page.
        DailyBarsQuery((symbol,), cutoff.date(), cutoff.date(), "-", "split")
        ref = _label(subject["identity_ref"])
        cik = SecEdgarQuery("companyfacts", subject["cik"]).cik
        price_notes, price_problem = _price(symbol, observations, problem)
        sec_notes, sec_problem = _sec(subject, load, cutoff)
        reasons = [reason for reason in (price_problem, sec_problem) if reason is not None]
        state = "hold_for_evidence" if reasons else "research_candidate"
        lines.extend(
            ["", f"## {symbol} | {state}", f"- 固定した発行体 CIK `{cik}`（対応根拠 `{ref}`）"]
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

    query = DailyBarsQuery(("AAPL", "NVDA"), start, days[-1], "-", "split")
    page = add(
        "alpaca.json",
        # The native terminal-page cursor is JSON null, not a credential.
        {"bars": {"AAPL": bars, "NVDA": bars[-1:]}, "next_page_token": None},  # nosec B105
        request_url=query.url(),
    )
    accession = "0000000000-26-000001"
    fact = {
        "accn": accession,
        "form": "10-Q",
        "start": "2026-07-01",
        "end": "2026-09-30",
        "val": 1234567890,
    }
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
        request_url=SecEdgarQuery("companyfacts", "0000320193").url,
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
                "identity_ref": "fixture-only-apple",
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
                "identity_ref": "fixture-only-nvidia",
                **period,
                "companyfacts": None,
                "filing": None,
            },
        ],
    }, files


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle", type=Path, help="fixed JSON manifest and adjacent original response byte files"
    )
    args = parser.parse_args(argv)
    manifest: Path | None = args.bundle
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
            report = render_research(bundle, read_native, synthetic=False)
    except (KeyError, TypeError, ValueError, OSError, ArithmeticError):
        parser.error("invalid fixed US research receipt bundle")
    print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
