"""同一sealed市場の対象観測だけに、検証済みの参考EPS原本を結び付ける。"""

from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from collections.abc import Mapping
from datetime import date, datetime
from gzip import BadGzipFile
from zlib import error as DeflateError

from baibai_engine.data_collection.receipts.public_pages.receipts import read_responses

from .models import ModelValuationObservation, MonexEPSReceipt, utc_datetime
from .monex_receipt import project_monex_eps
from .normalization import monex

_DAY_INDEX = "idx_equity_web_source_responses_received_on"
_DAY_KEYS = """
    SELECT requested_ticker,received_at_utc,requested_url,receipt_id
    FROM equity_web_source_responses INDEXED BY idx_equity_web_source_responses_received_on
    WHERE received_on=? AND source='monex' AND role='page' AND parent_receipt_id IS NULL
"""


def read_generation_monex_eps(
    connection: sqlite3.Connection,
    *,
    latest: Mapping[str, ModelValuationObservation],
    cutoff: datetime,
    as_of: date,
) -> dict[str, MonexEPSReceipt]:
    """同一seal/transactionはcallerが保持。世代ごとに最新観測のUTC日だけを索引で一度ずつ読む。"""
    cutoff = utc_datetime(cutoff)
    wanted: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for ticker, price in latest.items():
        if (
            price.distributor == "monex"
            and ticker == price.ticker
            and price.parser_version == monex.PARSER_VERSION
            and price.source_url == monex.public_url(ticker)
            and price.fetched_at_utc <= cutoff
            and price.snapshot_date <= as_of
        ):
            stamp = price.fetched_at_utc
            wanted[stamp.date().isoformat()].add(
                (ticker, stamp.isoformat(timespec="microseconds"), price.source_url)
            )
    found: dict[str, MonexEPSReceipt] = {}
    days = unique_reads = opened = raw_bytes = ambiguous = failed = 0

    def report() -> dict[str, MonexEPSReceipt]:
        # Aggregate counts only. Do not log receipt IDs, tickers, bodies or EPS values.
        print(
            f"model inputs: phase=monex_fiscal_eps eligible={sum(map(len, wanted.values()))} "
            f"indexed_days={days} unique_body_reads={unique_reads} decoded_bodies={opened} "
            f"decoded_bytes={raw_bytes} ambiguous={ambiguous} failed={failed} "
            f"accepted={len(found)}",
            file=sys.stderr,
            flush=True,
        )
        return found

    if (
        not wanted
        or not connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='index' AND name=?", (_DAY_INDEX,)
        ).fetchone()
    ):
        return report()
    for day, keys in wanted.items():
        days += 1
        unique: dict[str, str | None] = {}
        for ticker, stamp_text, url, receipt_id in connection.execute(_DAY_KEYS, (day,)):
            if (ticker, stamp_text, url) in keys:
                unique[ticker] = None if ticker in unique else receipt_id
        # 日内の重複もすべて確定してから、選ばれた原本の圧縮本体だけを主キーで展開する。
        for ticker, identity in unique.items():
            if identity is None:
                ambiguous += 1
                continue
            unique_reads += 1
            try:
                (receipt,) = read_responses(connection, (identity,))
                opened += 1
                raw_bytes += len(receipt.body)
                eps = project_monex_eps(
                    receipt, ticker=ticker, cutoff=cutoff, expected_prices=latest[ticker]
                )
            except (ValueError, EOFError, BadGzipFile, DeflateError):
                failed += 1
                continue
            ref = eps.reference_eps
            if ref and any(
                period and period.eps_jpy_per_share is not None
                for period in (ref.current_fy, ref.next_fy)
            ):
                found[ticker] = eps
    return report()
