"""Bounded public PMI article reads; never traverse private/chart API endpoints."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime, timedelta

from baibai_engine.data_collection.catalog import SourceDefinition
from baibai_engine.data_collection.provider_contracts import ProviderSpec
from baibai_engine.data_collection.providers.tradingeconomics.pmi_contract import (
    BASE_URL,
    STREAM_PATHS,
    article_urls,
    parse_article,
    request_key,
)
from baibai_engine.data_collection.receipts.native_sources.documents import SourceDocument
from baibai_engine.data_collection.transport.context import (
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    ProviderFetchStoppedError,
    SourceWithheldError,
    fetch_bytes,
)

_MAX_HTML_BYTES = 4 * 1024 * 1024


class TradingEconomicsPmiSource:
    # Retained official PDF history shares the unchanged measurement source IDs.
    spec = ProviderSpec(name="tradingeconomics_pmi")
    name = spec.name

    def fetch_documents(
        self,
        source: SourceDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None,
        existing: Mapping[str, SourceDocument],
        force: bool,
        recheck_existing: bool = True,
        selected_urls: tuple[str, ...] | None = None,
    ) -> Iterator[SourceDocument]:
        stream = source.provider_series_id
        known = {
            document.source_url
            for document in existing.values()
            if document.metadata.get("delivery_provider") == "tradingeconomics"
            and any(
                isinstance(month, str) and start.isoformat() <= month <= end.isoformat()
                for month in (
                    document.metadata.get("observed_at"),
                    document.metadata.get("requested_observed_at"),
                )
            )
        }
        failures: list[str] = []
        if selected_urls is not None:
            # Explicit finite input, never an automatic alternative after a refusal.
            pattern = re.compile(re.escape(BASE_URL + STREAM_PATHS[stream]) + r"/news/\d+\Z")
            if not selected_urls or any(not pattern.fullmatch(url) for url in selected_urls):
                raise ValueError("explicit TE PMI article URLs must belong to the requested stream")
            discovered = selected_urls
            known = set()
        else:
            try:
                index = fetch_bytes(
                    session,
                    BASE_URL + STREAM_PATHS[stream],
                    params=None,
                    max_bytes=_MAX_HTML_BYTES,
                    context=context,
                )
                discovered = article_urls(index, stream=stream)
                if not discovered:
                    failures.append("TE PMI index has no public article links")
            except ProviderFetchStoppedError:
                raise
            except SourceWithheldError as error:
                raise ProviderFetchStoppedError(str(error)) from error
            except IndicatorsProviderError as error:
                discovered = ()
                failures.append(str(error))
        latest = dict(existing)
        for url in sorted(known | set(discovered)):
            key = request_key(url)
            if (
                not recheck_existing
                and not force
                and key in existing
                and "identity_error" not in existing[key].metadata
                and existing[key].metadata.get("phase", "final") == "final"
                and existing[key].metadata.get(
                    "requested_observed_at", existing[key].metadata.get("observed_at")
                )
                == existing[key].metadata.get("observed_at")
            ):
                continue
            try:
                body = fetch_bytes(
                    session,
                    url,
                    params=None,
                    max_bytes=_MAX_HTML_BYTES,
                    context=context,
                )
            except ProviderFetchStoppedError:
                raise
            except SourceWithheldError as error:
                raise ProviderFetchStoppedError(str(error)) from error
            except IndicatorsProviderError as error:
                failures.append(str(error))
                continue
            metadata: dict[str, object] = {
                "delivery_provider": "tradingeconomics",
                "measurement_provider": "spglobal",
                "stream": stream,
            }
            try:
                article = parse_article(body, stream=stream, url=url)
            except ValueError as error:
                # The raw reception remains available even when native identity fails.
                metadata["identity_error"] = str(error)
                prior = existing.get(key)
                if prior is not None:
                    requested = prior.metadata.get(
                        "requested_observed_at", prior.metadata.get("observed_at")
                    )
                    if requested is not None:
                        metadata["requested_observed_at"] = requested
                failures.append(f"{url}: {error}")
            else:
                if not start <= article.observed_at <= end and key not in existing:
                    continue
                prior = existing.get(key)
                if prior is not None:
                    requested = prior.metadata.get(
                        "requested_observed_at", prior.metadata.get("observed_at")
                    )
                    if requested is not None and requested != article.observed_at.isoformat():
                        metadata["requested_observed_at"] = requested
                metadata.update(
                    {
                        "observed_at": article.observed_at.isoformat(),
                        "phase": article.phase,
                        "published_at": article.published_at.isoformat(),
                        "modified_at": article.modified_at.isoformat(),
                    }
                )
            document = SourceDocument(
                source.source_id,
                key,
                url,
                "text/html",
                datetime.now(UTC),
                body,
                metadata,
            )
            latest[key] = document
            yield document
        covered = {
            str(document.metadata.get("observed_at"))
            for document in latest.values()
            if document.metadata.get("phase", "final") == "final"
        }
        first = start
        if first.day != 1:
            first = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        expected = end.replace(day=1)
        for _ in range(1 if end.day >= 10 else 2):
            expected = (expected - timedelta(days=1)).replace(day=1)
        while first <= expected:
            if first.isoformat() not in covered:
                failures.append(f"PMI final original is missing for {first.isoformat()}")
            first = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        if failures:
            raise IndicatorsProviderError(
                f"TE PMI incomplete: {len(failures)} article/discovery/coverage failures; "
                + "; ".join(failures[:3])
            )
