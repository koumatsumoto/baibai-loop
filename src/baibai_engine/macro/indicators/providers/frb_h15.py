from __future__ import annotations

import csv
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..db import ObservationRecord
from ..definitions import SeriesDefinition
from .base import (
    MAX_CSV_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    ProviderSpec,
    SourceWithheldError,
    fetch_text,
    parse_optional_float,
    record_observation,
    store_fetched_bytes,
)

# Single H.15 package that carries every Treasury constant-maturity series; one
# download via FetchContext.bytes_cache serves all frb_h15 series in a run.
_H15_PACKAGE_SERIES = "bf17364827e38702b42a58cf8eaa3f78"
# federalreserve.gov's edge bot-mitigation withholds the CSV from datacenter IPs
# (a hosted batch run) whose requests do not look like a real browser navigation,
# answering with an HTML block page, an empty body, or a 403 challenge. Sending
# the companion Accept / Accept-Language headers a real Chrome sends alongside its
# User-Agent reduces the block rate; a blocked fetch falls back to a real browser,
# which the edge cannot tell from a human visitor.
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


class FrbH15Provider:
    """Federal Reserve H.15 package CSV. Single series or a left-right spread."""

    spec = ProviderSpec(name="frb_h15", all_history_start=date(1962, 1, 1))
    name = spec.name

    def fetch(
        self,
        series: SeriesDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None = None,
    ) -> list[ObservationRecord]:
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
    """The H.15 package CSV, from the plain client or a real browser.

    The plain client serves the common case. The edge withholds the CSV in three
    shapes — an empty body, a 403, or a 2xx carrying a block page — and the last
    is indistinguishable from data by status alone, so it is recognised by the
    CSV header being absent. All three re-fetch through a browser navigation,
    which the edge cannot tell from a human visitor. Nothing else falls back: a
    size cap, a 404, a 5xx or a network fault means the source is not withholding
    anything, and re-fetching the same resource a slower way would answer a
    question nobody asked.

    Without a context there is no browser to fall back to, so the plain response
    is returned and fails in the parser with the snippet naming what arrived.
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
    if navigation_url in context.blocked_browser_urls:
        raise IndicatorsProviderError(
            f"browser fetch of {url} already failed earlier in this pass; "
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
        # and each attempt costs a navigation timeout. Recording the block keeps
        # the remaining series and the retry from paying it again. The decode is
        # wrapped here so a non-UTF-8 answer fails like every other bad response
        # rather than escaping as an error the refresh cannot retry.
        context.blocked_browser_urls.add(navigation_url)
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


def parse_h15_csv(
    series: SeriesDefinition, text: str, *, start: date, end: date
) -> list[ObservationRecord]:
    lines = text.splitlines()
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
    reader = csv.DictReader(lines[header_index:])
    fieldnames = set(reader.fieldnames or ())
    observations: list[ObservationRecord] = []
    left_id, right_id = _split_provider_series_id(series.provider_series_id)
    if left_id not in fieldnames:
        raise IndicatorsProviderError(f"FRB H.15 CSV missing column {left_id}")
    if right_id is not None and right_id not in fieldnames:
        raise IndicatorsProviderError(f"FRB H.15 CSV missing column {right_id}")
    for row in reader:
        observed_at_raw = row.get("Time Period")
        if not observed_at_raw:
            continue
        observed_at = date.fromisoformat(observed_at_raw)
        if not start <= observed_at <= end:
            continue
        left_value = parse_optional_float(row.get(left_id))
        if left_value is None:
            continue
        if right_id is None:
            value = left_value
        else:
            right_value = parse_optional_float(row.get(right_id))
            if right_value is None:
                continue
            value = (left_value - right_value) * 100.0
        observations.append(record_observation(series, observed_at=observed_at, value=value))
    return observations


def _split_provider_series_id(provider_series_id: str) -> tuple[str, str | None]:
    if "-" not in provider_series_id:
        return provider_series_id, None
    left, right = provider_series_id.split("-", maxsplit=1)
    return left, right
