"""Synthetic prose/HTML exercises the public route without redistributing articles."""

import json
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

import baibai_engine.data_collection.acquisition.refresh as acquisition
import baibai_engine.data_collection.providers.tradingeconomics.pmi as te
from baibai_engine.data_collection.catalog import load_sources, require_source
from baibai_engine.data_collection.receipts.native_sources import store
from baibai_engine.data_collection.receipts.native_sources.documents import (
    SourceDocument,
    read_documents,
    store_document,
)
from baibai_engine.data_collection.transport.context import (
    FetchContext,
    IndicatorsProviderError,
    ProviderRateLimitError,
    SourceWithheldError,
)
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
    load_definitions,
)
from baibai_engine.machine_analysis.observations.economic_indicators.documents import (
    read_document_series,
)

SEP = date(2026, 9, 1)
END = date(2026, 10, 29)
WHEN = datetime(2026, 10, 29, tzinfo=UTC)


@pytest.fixture
def te_catalog(monkeypatch):
    catalog = {
        key: replace(
            source,
            provider="tradingeconomics_pmi",
            source_url=te.BASE_URL + te.STREAM_PATHS[source.provider_series_id],
        )
        if source.provider == "spglobal_pmi"
        else source
        for key, source in load_sources().items()
    }
    monkeypatch.setattr(acquisition, "load_sources", lambda: catalog)
    return catalog


URL = te.BASE_URL + "/japan/manufacturing-pmi/news/100"
FINAL = "S&P Global Japan Manufacturing PMI was confirmed at 54.1 in September 2026."
FLASH = (
    "S&P Global Japan Manufacturing PMI rose to 54.1 in September 2026, "
    "according to a flash estimate."
)


def html(lead, *, url=URL, published="2026-10-01T00:35:15Z", modified=None, related=""):
    metadata = {
        "@type": "Article",
        "url": url,
        "datePublished": published,
        "dateModified": modified or published,
    }
    return (
        '<h1 class="news_title">Synthetic article</h1>'
        f'<div class="news_description"><p>{lead}</p></div>'
        f'<script type="application/ld+json">{json.dumps(metadata)}</script>'
        f'<div class="news_stream">{related}</div>'
    ).encode()


def document(body, *, url=URL, stream="jp_manufacturing", when=WHEN):
    source_id = {
        "jp_manufacturing": "spglobal-jp-pmi-mfg",
        "jp_services": "spglobal-jp-pmi-svc",
        "us_manufacturing": "spglobal-us-pmi-mfg",
        "us_services": "spglobal-us-pmi-svc",
    }[stream]
    article = te.parse_article(body, stream=stream, url=url)
    return SourceDocument(
        source_id,
        te.request_key(url),
        url,
        "text/html",
        when,
        body,
        {
            "delivery_provider": "tradingeconomics",
            "measurement_provider": "spglobal",
            "stream": stream,
            "observed_at": article.observed_at.isoformat(),
            "phase": article.phase,
            "published_at": article.published_at.isoformat(),
            "modified_at": article.modified_at.isoformat(),
        },
    )


def acquire(monkeypatch, replies, *, existing=None, start=SEP, end=END):
    calls = []

    def fetch(session, url, **kwargs):
        calls.append(url)
        result = replies[url]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(te, "fetch_bytes", fetch)
    with FetchContext() as context:
        result = list(
            te.TradingEconomicsPmiSource().fetch_documents(
                require_source("spglobal-jp-pmi-mfg"),
                start=start,
                end=end,
                session=context.session,
                context=context,
                existing=existing or {},
                force=False,
            )
        )
    return result, calls


@pytest.mark.parametrize(
    ("stream", "lead", "expected"),
    [
        ("jp_manufacturing", FINAL, 54.1),
        (
            "jp_services",
            "Japan's S&P Global Services PMI Business Activity Index stood at 51.3 in September 2026, below a flash reading of 51.6.",
            51.3,
        ),
        (
            "us_manufacturing",
            "S&P Global US Manufacturing PMI fell to 55.9 in September 2026 from a preliminary estimate of 57.0.",
            55.9,
        ),
        (
            "us_services",
            "S&P Global US Services PMI rose to 58.8 in September 2026, above the flash estimate of 58.7.",
            58.8,
        ),
    ],
)
def test_four_series_primary_article_excludes_newer_related_month(tmp_path, stream, lead, expected):
    doc = document(html(lead, related="October 2026 flash PMI rose to 99.0"), stream=stream)
    series = next(item for item in load_definitions().series if item.reading_key == stream)
    with closing(store.initialize_database(tmp_path / "source.sqlite")) as conn, conn:
        store_document(conn, doc)
        values = read_document_series(conn, series, start=SEP, end=END)
    assert [(item.observed_at, item.value) for item in values] == [(SEP, expected)]
    assert values[0].quality_flags == ("secondary_delivery", "final")
    assert values[0].vintage_at == WHEN
    assert "delivery_published_at=2026-10-01T00:35:15+00:00" in values[0].notes


@pytest.mark.parametrize(
    "lead",
    [
        "S&P Global US Manufacturing PMI was confirmed at 54.1 in September 2026.",
        "S&P Global Japan Manufacturing PMI Output Index was confirmed at 54.1 in September 2026.",
        "S&P Global Japan Composite PMI was confirmed at 54.1 in September 2026.",
        "S&P Global Japan Manufacturing PMI was confirmed at 54.1 in November 2026.",
        "S&P Global Japan Manufacturing PMI is expected to reach 54.1 in September 2026.",
    ],
)
def test_wrong_country_index_month_or_unestablished_phase_is_not_repaired(lead):
    with pytest.raises(ValueError, match="TE PMI"):
        te.parse_article(html(lead), stream="jp_manufacturing", url=URL)


def test_flash_final_same_value_late_flash_and_current_month_remain_one_sample(tmp_path):
    flash_url = URL[:-3] + "101"
    october_url = URL[:-3] + "102"
    docs = [
        document(html(FINAL)),
        document(
            html(FLASH, url=flash_url, published="2026-09-24T00:30:00Z"),
            url=flash_url,
            when=WHEN + timedelta(seconds=1),
        ),
        document(
            html(
                FLASH.replace("September", "October").replace("54.1", "53.3"),
                url=october_url,
                published="2026-10-23T00:30:00Z",
            ),
            url=october_url,
        ),
    ]
    series = load_definitions().by_id()["jp.pmi_manufacturing"]
    with closing(store.initialize_database(tmp_path / "source.sqlite")) as conn, conn:
        for doc in docs:
            store_document(conn, doc)
        values = read_document_series(conn, series, start=SEP, end=END)
    assert [(item.observed_at, item.value) for item in values] == [
        (SEP, 54.1),
        (date(2026, 10, 1), 53.3),
    ]
    assert values[0].quality_flags[-1] == "final"
    assert values[1].quality_flags[-1] == "flash"


def test_same_url_correction_rechecked_with_discovery_and_retained_history(tmp_path, monkeypatch):
    index = te.BASE_URL + te.STREAM_PATHS["jp_manufacturing"]
    old = document(html(FINAL))
    new_body = html(FINAL.replace("54.1", "54.2"), modified="2026-10-02T01:00:00Z")
    # The known URL is deliberately absent from today's index.
    another = URL[:-3] + "102"
    latest = html(
        FLASH.replace("September", "October"), url=another, published="2026-10-23T00:30:00Z"
    )
    docs, calls = acquire(
        monkeypatch,
        {index: f'<a href="{another}">latest</a>'.encode(), URL: new_body, another: latest},
        existing={old.request_key: old},
    )
    assert set(calls) == {index, URL, another}
    series = load_definitions().by_id()["jp.pmi_manufacturing"]
    with closing(store.initialize_database(tmp_path / "source.sqlite")) as conn, conn:
        store_document(conn, old)
        for doc in docs:
            store_document(conn, replace(doc, fetched_at=WHEN + timedelta(seconds=1)))
        assert conn.execute("SELECT count(*) FROM economic_source_documents").fetchone()[0] == 3
        assert read_document_series(conn, series, start=SEP, end=SEP)[0].value == 54.2
        assert old.payload == html(FINAL)


def test_current_month_flash_is_accepted_without_next_month_final_requirement(monkeypatch):
    index = te.BASE_URL + te.STREAM_PATHS["jp_manufacturing"]
    body = html(FLASH.replace("September", "October"), published="2026-10-23T00:30:00Z")
    docs, _ = acquire(
        monkeypatch,
        {index: f'<a href="{URL}">latest</a>'.encode(), URL: body},
        start=date(2026, 10, 1),
    )
    assert docs[0].metadata["phase"] == "flash"


def test_existing_pdf_history_is_not_fetched_from_the_refused_route(monkeypatch):
    index = te.BASE_URL + te.STREAM_PATHS["jp_manufacturing"]
    old_month = date(2026, 8, 1)
    old = SourceDocument(
        "spglobal-jp-pmi-mfg",
        old_month.isoformat(),
        "https://www.pmi.spglobal.com/old",
        "application/pdf",
        WHEN,
        b"%PDF untouched",
        {"observed_at": old_month.isoformat(), "release_observed_at": old_month.isoformat()},
    )
    docs, calls = acquire(
        monkeypatch,
        {index: f'<a href="{URL}">final</a>'.encode(), URL: html(FINAL)},
        existing={old.request_key: old},
        start=old_month,
    )
    assert len(docs) == 1
    assert old.source_url not in calls


@pytest.mark.parametrize("error", [SourceWithheldError("HTTP 403"), ProviderRateLimitError("429")])
def test_refusal_stops_siblings_without_fallback(tmp_path, monkeypatch, error, te_catalog):
    calls = []

    def fetch(session, url, **kwargs):
        calls.append(url)
        raise error

    monkeypatch.setattr(te, "fetch_bytes", fetch)
    monkeypatch.setattr(acquisition, "fetch_native_records", lambda *a, **k: {})
    outcomes = acquisition.refresh_sources(
        ["spglobal-jp-pmi-mfg", "spglobal-us-pmi-svc", "mof-jgbcm-all"],
        db_path=tmp_path / "source.sqlite",
        start=SEP,
        end=END,
    )
    assert len(calls) == 1
    assert isinstance(outcomes[0], acquisition.SourceRefreshFailure)
    assert isinstance(outcomes[1], acquisition.SourceRefreshFailure)
    assert isinstance(outcomes[2], acquisition.SourceRefreshSuccess)


def test_partial_failure_preserves_received_article_and_marks_failed_run(
    tmp_path, monkeypatch, te_catalog
):
    index = te.BASE_URL + te.STREAM_PATHS["jp_manufacturing"]
    broken = URL[:-3] + "999"

    def fetch(session, url, **kwargs):
        if url == index:
            return f'<a href="{URL}">first</a><a href="{broken}">second</a>'.encode()
        if url == URL:
            return html(FINAL)
        raise IndicatorsProviderError("HTTP 503")

    monkeypatch.setattr(te, "fetch_bytes", fetch)
    path = tmp_path / "source.sqlite"
    outcome = acquisition.refresh_sources(
        ["spglobal-jp-pmi-mfg"], db_path=path, start=SEP, end=END
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshFailure)
    with closing(store.open_read_only_connection(path)) as conn:
        assert [item.source_url for item in read_documents(conn, "spglobal-jp-pmi-mfg")] == [URL]
        assert tuple(
            conn.execute("SELECT status, record_count FROM economic_source_runs").fetchone()
        ) == ("failed", 1)


def test_native_identity_failure_still_retains_raw_html(tmp_path, monkeypatch, te_catalog):
    index = te.BASE_URL + te.STREAM_PATHS["jp_manufacturing"]
    monkeypatch.setattr(
        te,
        "fetch_bytes",
        lambda session, url, **k: (
            f'<a href="{URL}">article</a>'.encode() if url == index else b"<html>changed</html>"
        ),
    )
    path = tmp_path / "source.sqlite"
    outcome = acquisition.refresh_sources(
        ["spglobal-jp-pmi-mfg"], db_path=path, start=SEP, end=END
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshFailure)
    with closing(store.open_read_only_connection(path)) as conn:
        doc = next(read_documents(conn, "spglobal-jp-pmi-mfg"))
        assert doc.payload == b"<html>changed</html>"
        assert "identity_error" in doc.metadata
        series = load_definitions().by_id()["jp.pmi_manufacturing"]
        with pytest.raises(ValueError, match="main article"):
            read_document_series(conn, series, start=SEP, end=END)


def test_explicit_known_urls_do_not_contact_discovery_index(monkeypatch):
    calls = []

    def fetch(session, url, **kwargs):
        calls.append(url)
        return html(FINAL)

    monkeypatch.setattr(te, "fetch_bytes", fetch)
    with FetchContext() as context:
        docs = list(
            te.TradingEconomicsPmiSource().fetch_documents(
                require_source("spglobal-jp-pmi-mfg"),
                start=SEP,
                end=END,
                session=context.session,
                context=context,
                existing={},
                force=False,
                selected_urls=(URL,),
            )
        )
    assert calls == [URL]
    assert docs[0].metadata["phase"] == "final"


@pytest.mark.parametrize("recheck_existing", [True, False])
def test_malformed_same_url_outside_index_stays_recheckable_and_can_repair(
    tmp_path, monkeypatch, recheck_existing
):
    from baibai_engine.data_collection.acquisition.policy import _missing_ranges

    index = te.BASE_URL + te.STREAM_PATHS["jp_manufacturing"]
    october = URL[:-3] + "102"
    october_body = html(
        FLASH.replace("September", "October"),
        url=october,
        published="2026-10-23T00:30:00Z",
    )
    replies = {
        index: f'<a href="{october}">latest only</a>'.encode(),
        URL: b"<html>publisher repair pending</html>",
        october: october_body,
    }
    calls = []

    def fetch(session, url, **kwargs):
        calls.append(url)
        return replies[url]

    monkeypatch.setattr(te, "fetch_bytes", fetch)
    source = replace(require_source("spglobal-jp-pmi-mfg"), provider="tradingeconomics_pmi")
    original = document(html(FINAL))
    received = []
    with FetchContext() as context, pytest.raises(IndicatorsProviderError, match="missing"):
        received.extend(
            replace(doc, fetched_at=WHEN + timedelta(seconds=1))
            for doc in te.TradingEconomicsPmiSource().fetch_documents(
                source,
                start=SEP,
                end=END,
                session=context.session,
                context=context,
                existing={original.request_key: original},
                force=False,
            )
        )
    malformed = next(doc for doc in received if doc.source_url == URL)
    assert malformed.metadata["requested_observed_at"] == SEP.isoformat()
    assert "observed_at" not in malformed.metadata
    series = load_definitions().by_id()["jp.pmi_manufacturing"]
    with closing(store.initialize_database(tmp_path / "source.sqlite")) as conn, conn:
        store_document(conn, original)
        for doc in received:
            store_document(conn, doc)
        with pytest.raises(ValueError, match="main article"):
            read_document_series(conn, series, start=SEP, end=SEP)
        assert _missing_ranges(conn, source, SEP, date(2026, 9, 30), []) == (
            (SEP, date(2026, 9, 30)),
        )
        existing = {doc.request_key: doc for doc in read_documents(conn, source.source_id)}
        replies[URL] = html(FINAL.replace("54.1", "54.3"), modified="2026-10-29T01:00:00Z")
        calls.clear()
        with FetchContext() as context:
            repaired = list(
                te.TradingEconomicsPmiSource().fetch_documents(
                    source,
                    start=SEP,
                    end=END,
                    session=context.session,
                    context=context,
                    existing=existing,
                    force=False,
                    recheck_existing=recheck_existing,
                )
            )
        assert URL in calls
        for doc in repaired:
            store_document(conn, replace(doc, fetched_at=WHEN + timedelta(seconds=2)))
        assert read_document_series(conn, series, start=SEP, end=SEP)[0].value == 54.3


def test_comparison_month_cannot_become_current_headline_month():
    lead = (
        "The S&P Global US Services PMI rose to 58.8 from 56.5 in August 2026, "
        "final figures for September 2026 showed."
    )
    with pytest.raises(ValueError, match="country/index/month"):
        te.parse_article(html(lead), stream="us_services", url=URL)


@pytest.mark.parametrize("recheck_existing", [True, False])
def test_known_url_corrected_to_out_of_range_month_removes_stale_month(
    tmp_path, monkeypatch, recheck_existing
):
    index = te.BASE_URL + te.STREAM_PATHS["jp_manufacturing"]
    october = URL[:-3] + "102"
    replies = {
        index: f'<a href="{october}">latest only</a>'.encode(),
        URL: html(FINAL.replace("September", "August")),
        october: html(
            FLASH.replace("September", "October"), url=october, published="2026-10-23T00:30:00Z"
        ),
    }
    calls = []

    def fetch(session, url, **kwargs):
        calls.append(url)
        return replies[url]

    monkeypatch.setattr(te, "fetch_bytes", fetch)
    original = document(html(FINAL))
    source = require_source("spglobal-jp-pmi-mfg")
    received = []
    with FetchContext() as context, pytest.raises(IndicatorsProviderError, match="2026-09-01"):
        received.extend(
            replace(doc, fetched_at=WHEN + timedelta(seconds=1))
            for doc in te.TradingEconomicsPmiSource().fetch_documents(
                source,
                start=SEP,
                end=END,
                session=context.session,
                context=context,
                existing={original.request_key: original},
                force=False,
            )
        )
    changed = next(doc for doc in received if doc.source_url == URL)
    assert changed.metadata["observed_at"] == "2026-08-01"
    assert changed.metadata["requested_observed_at"] == "2026-09-01"
    series = load_definitions().by_id()["jp.pmi_manufacturing"]
    with closing(store.initialize_database(tmp_path / "source.sqlite")) as conn, conn:
        store_document(conn, original)
        for doc in received:
            store_document(conn, doc)
        assert read_document_series(conn, series, start=SEP, end=SEP) == ()
        existing = {doc.request_key: doc for doc in read_documents(conn, source.source_id)}
        replies[URL] = html(FINAL)
        calls.clear()
        with FetchContext() as context:
            list(
                te.TradingEconomicsPmiSource().fetch_documents(
                    source,
                    start=SEP,
                    end=END,
                    session=context.session,
                    context=context,
                    existing=existing,
                    force=False,
                    recheck_existing=recheck_existing,
                )
            )
        assert URL in calls
