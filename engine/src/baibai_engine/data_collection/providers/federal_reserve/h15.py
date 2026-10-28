from __future__ import annotations

import csv
import io
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from baibai_engine.data_collection.catalog import SourceDefinition
from baibai_engine.data_collection.provider_contracts import (
    FRB_H15_PACKAGE_SERIES,
    ProviderSpec,
)
from baibai_engine.data_collection.receipts.native_sources.records import NativeRecords
from baibai_engine.data_collection.transport.context import (
    MAX_CSV_RESPONSE_BYTES,
    BrowserUnavailableError,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    SourceWithheldError,
    fetch_text,
    store_fetched_bytes,
)

# Single H.15 package that carries every Treasury constant-maturity series; one
# download via FetchContext.bytes_cache serves all frb_h15 series in a run.
_H15_PACKAGE_SERIES = FRB_H15_PACKAGE_SERIES
# The public CSV may return an empty body or HTML rather than data. Headers and
# a browser response are transport alternatives, not evidence of access policy.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_BROWSER_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/csv,text/plain,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_HEADER_PREFIXES = ('"Time Period"', "Time Period,")


class FrbH15Source:
    """Federal Reserve H.15 source yields, without local spread calculations."""

    spec = ProviderSpec(name="frb_h15", all_history_start=date(1962, 1, 1))
    name = spec.name

    def fetch_records(
        self,
        series: SourceDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None = None,
    ) -> NativeRecords:
        params = {
            "rel": "H15",
            "series": _H15_PACKAGE_SERIES,
            "lastObs": "",
            "from": start.strftime("%m/%d/%Y"),
            "to": end.strftime("%m/%d/%Y"),
            "filetype": "csv",
            "label": "include",
            "layout": "seriescolumn",
            "type": "package",
        }
        text = _fetch_h15_csv(series.source_url, params, session=session, context=context)
        return parse_h15_csv(series, text, start=start, end=end)


def _fetch_h15_csv(
    url: str,
    params: dict[str, str],
    *,
    session: HttpSession,
    context: FetchContext | None,
) -> str:
    """Try the configured public transport routes and validate the CSV header.

    Only an empty/403 response or missing CSV header selects the browser route.
    Other transport failures propagate; neither route guarantees source access.
    """

    plain_failure: str | None = None
    try:
        text = fetch_text(
            session,
            url,
            params=params,
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            headers=_BROWSER_HEADERS,
            context=context,
        )
    except SourceWithheldError as exc:
        if context is None:
            raise
        plain_failure = str(exc)
        text = ""
    if _has_header(text):
        return text
    if context is None:
        return text
    if plain_failure is None:
        plain_failure = f"response without the CSV header: {snippet(text)!r}"
    return _fetch_h15_csv_via_browser(url, params, context=context, plain_failure=plain_failure)


def _fetch_h15_csv_via_browser(
    url: str,
    params: dict[str, str],
    *,
    context: FetchContext,
    plain_failure: str,
) -> str:
    """The CSV read through a real browser, after the plain client was turned away.

    What the plain client was answered with travels into every failure here: it
    is the evidence for how the edge is blocking, only this message reaches the
    batch log, and without it a recurrence reads as a browser problem.
    """

    navigation_url = _navigation_url(url, params)
    blocked = context.blocked_browser_urls.get(navigation_url)
    if blocked is not None:
        raise IndicatorsProviderError(
            f"{blocked} (already seen earlier in this pass); "
            f"the plain client reported: {plain_failure}"
        )
    try:
        content = context.browser_fetcher().fetch_download(
            navigation_url, max_bytes=MAX_CSV_RESPONSE_BYTES
        )
        text = content.decode("utf-8-sig")
        if not _has_header(text):
            raise IndicatorsProviderError(
                f"browser response without the CSV header: {snippet(text)!r}"
            )
    except (IndicatorsProviderError, UnicodeDecodeError) as exc:
        # An edge that turned this navigation away turns the next one away too,
        # and each attempt costs a navigation timeout, so what the edge answered
        # is recorded and the remaining series report it without paying again. A
        # browser that never started says nothing about the edge, so it is not
        # recorded and the next series still tries. The decode is wrapped here so
        # a non-UTF-8 answer fails like every other bad response rather than
        # escaping as an error the refresh cannot retry.
        if not isinstance(exc, BrowserUnavailableError):
            context.blocked_browser_urls[navigation_url] = str(exc)
        raise IndicatorsProviderError(
            f"{exc}; the plain client first failed with: {plain_failure}"
        ) from exc
    # Every frb_h15 series in the pass shares this one download, so the browser
    # result enters the same cache the plain client fills, under the same guards.
    # Skipping this would cost one browser navigation per series.
    store_fetched_bytes(context, url, params, content, max_bytes=MAX_CSV_RESPONSE_BYTES)
    return text


def _navigation_url(url: str, params: dict[str, str]) -> str:
    """``url`` with ``params`` in its query, preserving any query it already has.

    A browser navigation carries its parameters in the URL while the plain client
    passes them beside it, so this is the one place the two routes could address
    different resources.
    """

    parts = urlsplit(url)
    query = urlencode([*parse_qsl(parts.query), *params.items()])
    return urlunsplit(parts._replace(query=query))


def _has_header(text: str) -> bool:
    return any(line.startswith(_HEADER_PREFIXES) for line in text.splitlines())


def snippet(text: str) -> str:
    """What the source sent, flattened to one bounded line for a batch log."""

    return " ".join(text.split())[:160]


def parse_h15_csv(series: SourceDefinition, text: str, *, start: date, end: date) -> NativeRecords:
    lines = text.splitlines(keepends=True)
    header_index = next(
        (index for index, line in enumerate(lines) if line.startswith(_HEADER_PREFIXES)),
        None,
    )
    if header_index is None:
        # Surface what the server actually returned (block page, error HTML, or a
        # changed layout) so a remote-only failure is diagnosable from batch logs.
        raise IndicatorsProviderError(
            f"FRB H.15 CSV missing Time Period header; response starts with: {snippet(text)!r}"
        )
    reader = csv.DictReader(io.StringIO("".join(lines[header_index:])))
    columns = reader.fieldnames or []
    if len(columns) != len(set(columns)):
        raise IndicatorsProviderError("FRB H.15 CSV has duplicate columns")
    # Preserve package columns before any individual yield is selected in L2.
    # Preamble lines are literal source metadata, not an inferred schema/unit.
    preamble = [line.rstrip("\r\n") for line in lines[:header_index]]
    records: dict[date, list[dict[str, object]]] = {}
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise IndicatorsProviderError("FRB H.15 CSV row does not match its columns")
        observed_at_raw = row.get("Time Period")
        if not observed_at_raw:
            raise IndicatorsProviderError("FRB H.15 CSV row has no Time Period")
        observed_at = date.fromisoformat(observed_at_raw)
        if start <= observed_at <= end:
            records.setdefault(observed_at, []).append({"row": row, "preamble": preamble})
    return records
