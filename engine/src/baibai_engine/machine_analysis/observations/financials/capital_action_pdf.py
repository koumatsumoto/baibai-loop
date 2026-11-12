"""Conservatively identify completed issued-share changes in retained JPX PDFs."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import sqlite3
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from baibai_engine.machine_analysis.observations.financials.pdf_statements import (
    disclosure_identity,
)
from baibai_engine.primitives.time import JST

CAPITAL_ACTION_REVISION = "capital-actions-v2-post-period-completion"
_DATE = r"(20\d{2})年(\d{1,2})月(\d{1,2})日"

if TYPE_CHECKING:
    import duckdb


@dataclass(frozen=True, slots=True)
class CapitalAction:
    ticker: str
    issued_shares: int
    effective_date: date
    received_at: datetime
    source_url: str
    source_sha256: str
    disclosure_number: str
    description: str
    # Older/manual observations do not establish a separate publication date.
    published_on: date | None = None

    def after_as_of(self, as_of: date) -> bool:
        return self.effective_date > as_of or (
            self.published_on is not None and self.published_on > as_of
        )


def _subsequent_conversion(texts: Sequence[str], *, issuer: str | None) -> tuple[int, date] | None:
    if issuer is None:
        return None
    candidates: list[tuple[int, date]] = []
    for text in texts:
        if "重要な後発事象" not in text:
            continue
        completed = re.findall(
            r"当社は、[^。]*?当社が発行する([A-Z]種優先株式)について、[^。]*?"
            + _DATE
            + r"付で\1[\d,]+株を取得し、同日、取得した\1"
            r"(?:全て|すべて)を消却(?:いたしました|しました)",
            text,
        )
        if not completed:
            continue
        if len(completed) != 1:
            return None
        kind, year, month, day = completed[0]
        try:
            effective = date(int(year), int(month), int(day))
        except ValueError:
            return None
        # The completed class and exercise date must belong to the conversion
        # table on this page, not an earlier resolution or a separate cash call.
        sections = re.findall(r"転換の内容(.*?)\d+\.取得の内容", text)
        if len(sections) != 1 or set(re.findall(r"[A-Z]種優先株式", sections[0])) != {kind}:
            return None
        section = sections[0]
        if any(term in section for term in ("予定", "見込", "未定")):
            return None
        owners = re.findall(r"取得株式の種類(.*?)\d+取得請求権行使日", section)
        if owners != [issuer + kind]:
            return None
        dates = re.findall(r"取得請求権行使日" + _DATE, section)
        totals = re.findall(r"増加後の発行済み普通株式総数([\d,]+)株", section)
        increases = re.findall(r"転換により増加する普通株式数([\d,]+)株", section)
        if dates != [(year, month, day)] or len(totals) != 1 or len(increases) != 1:
            return None
        total, increase = (int(values[0].replace(",", "")) for values in (totals, increases))
        if not 0 < increase < total:
            return None
        candidates.append((total, effective))
    return candidates[0] if len(candidates) == 1 else None


def extract_completed_conversion(
    content: bytes, *, source_url: str, received_at: datetime, as_of: date
) -> CapitalAction | None:
    """Read explicit completed totals, never plans or inferred treasury balances."""
    ticker, disclosure_number = disclosure_identity(source_url)
    if received_at.utcoffset() is None:
        raise ValueError("capital action receipt requires a timezone")
    try:
        pages = PdfReader(io.BytesIO(content)).pages
        if not pages:
            return None
        texts = [
            re.sub(r"\s+", "", unicodedata.normalize("NFKC", page.extract_text() or ""))
            for page in pages
        ]
    except (PdfReadError, ValueError):
        return None
    header = texts[0]
    match = re.search(_DATE, header)
    if match is None or not re.search(
        r"コード(?:番号|:)" + re.escape(ticker) + r"(?:URL|[^0-9A-Z]|$)", header
    ):
        return None
    try:
        published = date(*(int(part) for part in match.groups()))
    except ValueError:
        return None
    # A later disclosure cannot enter an earlier price date merely because it
    # describes an earlier effective date. Receipt vintage is checked separately.
    if published > min(as_of, received_at.astimezone(JST).date()):
        return None
    text = "".join(texts)
    issuers = re.findall(r"上場会社名(.+?)上場取引所", header)
    subsequent = _subsequent_conversion(texts, issuer=issuers[0] if len(issuers) == 1 else None)
    same_day = re.findall(
        r"本日、本株式交付の効力が発生(?:し[、,]|しました[。.]|いたしました[。.])", text
    )
    if same_day:
        values = re.findall(
            r"本転換の結果、当(?:行|社)普通株式の発行済株式数は([\d,]+)株となり", text
        )
        if len(same_day) != 1 or len(values) != 1 or "重要な後発事象" in text:
            return None
        issued, effective = int(values[0].replace(",", "")), published
        description = "completed_common_share_conversion_total"
    elif subsequent is not None:
        issued, effective = subsequent
        description = "completed_post_period_common_share_conversion_total"
    else:
        return None
    if issued <= 0 or effective > published:
        return None
    return CapitalAction(
        ticker=ticker,
        issued_shares=issued,
        effective_date=effective,
        received_at=received_at,
        source_url=source_url,
        source_sha256=hashlib.sha256(content).hexdigest(),
        disclosure_number=disclosure_number,
        description=description,
        published_on=published,
    )


def read_capital_actions(
    connection: sqlite3.Connection | duckdb.DuckDBPyConnection,
    *,
    as_of: date,
    tickers: Sequence[str],
    known_at: datetime,
) -> dict[str, tuple[CapitalAction, ...]]:
    """Read only completed retained originals known by the generation cutoff."""
    if known_at.utcoffset() is None:
        raise ValueError("capital action cutoff requires timezone")
    selected = set(tickers)
    result: dict[str, list[CapitalAction]] = {}
    rows = connection.execute(
        "SELECT b.expected_resources_json, r.resource_name, r.requested_url, "
        "r.received_at_utc, r.content_type, r.body_gzip FROM jpx_source_bundles b "
        "JOIN jpx_source_responses r ON r.bundle_id=b.bundle_id "
        "WHERE b.collection_name='capital_actions' AND b.completed_at_utc IS NOT NULL "
        "ORDER BY r.received_at_utc, b.bundle_id, r.resource_name"
    )
    while batch := rows.fetchmany(8):
        for manifest, resource, url, received, media_type, compressed in batch:
            ticker, disclosure = disclosure_identity(str(url))
            if ticker not in selected:
                continue
            timestamp = datetime.fromisoformat(str(received))
            if timestamp.utcoffset() is None:
                raise ValueError("capital action receipt has no timezone")
            if timestamp > known_at:
                continue
            if (
                dict(json.loads(str(manifest))).get(resource) != url
                or resource != "pdf:" + disclosure
            ):
                raise ValueError("capital action original differs from source manifest")
            if str(media_type).split(";", 1)[0].strip().lower() != "application/pdf":
                raise ValueError("capital action PDF has an incompatible media type")
            action = extract_completed_conversion(
                gzip.decompress(bytes(compressed)),
                source_url=str(url),
                received_at=timestamp,
                as_of=as_of,
            )
            if action is not None and not action.after_as_of(as_of):
                result.setdefault(ticker, []).append(action)
    return {ticker: tuple(actions) for ticker, actions in result.items()}
