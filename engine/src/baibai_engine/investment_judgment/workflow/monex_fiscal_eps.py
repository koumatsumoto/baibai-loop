"""個別調査の固定世代に属するMonex原本だけを、追加取得なしで選ぶ。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import date, datetime
from gzip import BadGzipFile
from pathlib import Path
from zlib import error as DeflateError

from pydantic import ValidationError

from baibai_engine.data_collection.receipts.public_pages.receipts import read_responses
from baibai_engine.machine_analysis.generation.contracts import ModelInputSlice
from baibai_engine.machine_analysis.observations.external_assessments.models import (
    ModelValuationObservation,
)
from baibai_engine.machine_analysis.observations.external_assessments.monex_receipt import (
    MonexEPSReceipt,
    project_monex_eps,
)
from baibai_engine.machine_analysis.observations.external_assessments.normalization import monex
from baibai_engine.machine_analysis.observations.external_assessments.normalization.decode import (
    decode_attempt,
)
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    read_lake_store_origin_from_connection,
)
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only


def price_anchor(
    payload: Mapping[str, object], *, ticker: str, cutoff: datetime, as_of: date
) -> ModelValuationObservation | None:
    """過去の理論株価行を識別する。tickerだけで新しい別受領へ乗り換えない。"""
    rows = payload.get("model_valuations")
    known = payload.get("cutoff")
    if payload.get("ticker") != ticker or not isinstance(rows, list) or not isinstance(known, str):
        return None
    try:
        when = datetime.fromisoformat(known)
    except ValueError:
        return None
    if when.utcoffset() is None or when > cutoff:
        return None
    candidates = [
        latest
        for row in rows
        if isinstance(row, dict)
        and isinstance(inputs := row.get("inputs"), dict)
        and isinstance(latest := inputs.get("latest"), dict)
        and latest.get("distributor") == "monex"
    ]
    if len(candidates) != 1:
        return None
    try:
        observation = ModelValuationObservation.model_validate(candidates[0])
    except ValidationError:
        return None
    return (
        observation
        if (
            observation.ticker == ticker
            and observation.source_url == monex.public_url(ticker)
            and observation.fetched_at_utc <= when
            and observation.snapshot_date <= as_of
        )
        else None
    )


def matches_fiscal_eps(eps: MonexEPSReceipt, anchor: ModelValuationObservation | None) -> bool:
    reference = eps.reference_eps
    return bool(
        anchor is not None
        and (eps.ticker, eps.source_url, eps.received_at_utc)
        == (anchor.ticker, anchor.source_url, anchor.fetched_at_utc)
        and reference
        and any(
            period and period.eps_jpy_per_share is not None
            for period in (reference.current_fy, reference.next_fy)
        )
    )


def freeze_monex_fiscal_eps(
    path: Path | None,
    generation: ModelInputSlice,
    *,
    tickers: Sequence[str],
) -> dict[str, MonexEPSReceipt]:
    release = generation.inputs.get("market")
    if release is None or release.kind != "lake_release" or path is None or not path.is_file():
        return {}
    anchors = [
        anchor
        for ticker in tickers
        if (model := generation.result("external_assessment", ticker)) is not None
        and (
            anchor := price_anchor(
                model.payload,
                ticker=ticker,
                cutoff=generation.evaluation_time,
                as_of=generation.as_of,
            )
        )
        is not None
    ]
    if not anchors:
        return {}
    result = {}
    with closing(connect_read_only(path)) as conn:
        conn.execute("BEGIN")
        if not conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='equity_web_source_responses'"
        ).fetchone():
            return {}
        origin = read_lake_store_origin_from_connection(conn)
        if origin is None or (origin.release_id, origin.release_manifest_sha256) != (
            release.reference,
            release.sha256,
        ):
            return {}
        for anchor in anchors:
            stamp = anchor.fetched_at_utc
            ids = conn.execute(
                "SELECT receipt_id FROM equity_web_source_responses "
                "WHERE received_on=? AND received_at_utc=? AND source='monex' "
                "AND requested_ticker=? AND role='page' AND parent_receipt_id IS NULL "
                "AND requested_url=? LIMIT 2",
                (
                    stamp.date().isoformat(),
                    stamp.isoformat(timespec="microseconds"),
                    anchor.ticker,
                    anchor.source_url,
                ),
            ).fetchall()
            if len(ids) != 1:
                continue
            try:
                (receipt,) = read_responses(conn, (ids[0][0],))
                if (
                    decode_attempt((receipt,), expected_parser_version=anchor.parser_version)
                    != anchor
                ):
                    continue
                eps = project_monex_eps(receipt, ticker=anchor.ticker, cutoff=stamp)
            except (ValueError, EOFError, BadGzipFile, DeflateError):
                # An unresolvable, changed or unsupported original is not evidence for this case.
                continue
            if matches_fiscal_eps(eps, anchor):
                result[anchor.ticker] = eps
    return result
