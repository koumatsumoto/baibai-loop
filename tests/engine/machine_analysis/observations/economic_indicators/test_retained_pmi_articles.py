"""Read retained secondary PMI articles without an acquisition adapter."""

import json
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

import baibai_engine.data_collection.providers.tradingeconomics.pmi_contract as te
import baibai_engine.machine_analysis.observations.economic_indicators.pmi as pmi_reading
from baibai_engine.data_collection.receipts.native_sources import store
from baibai_engine.data_collection.receipts.native_sources.documents import (
    SourceDocument,
    store_document,
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
URL = "https://tradingeconomics.com/japan/manufacturing-pmi/news/100"
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
        source_id=source_id,
        request_key="te:" + url.rsplit("/", 1)[-1],
        source_url=url,
        content_type="text/html",
        fetched_at=when,
        payload=body,
        metadata={
            "delivery_provider": "tradingeconomics",
            "measurement_provider": "spglobal",
            "stream": stream,
            "observed_at": article.observed_at.isoformat(),
            "phase": article.phase,
            "published_at": article.published_at.isoformat(),
            "modified_at": article.modified_at.isoformat(),
        },
    )


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
def test_four_series_primary_article_excludes_newer_related_month(
    tmp_path, mocker, stream, lead, expected
):
    doc = document(html(lead, related="October 2026 flash PMI rose to 99.0"), stream=stream)
    parse = mocker.spy(pmi_reading, "parse_article")
    series = next(item for item in load_definitions().series if item.reading_key == stream)
    with closing(store.initialize_database(tmp_path / "source.sqlite")) as conn, conn:
        store_document(conn, doc)
        values = read_document_series(conn, series, start=SEP, end=END)
    parse.assert_called_once_with(doc.payload, stream=stream, url=doc.source_url)
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


def test_comparison_month_cannot_become_current_headline_month():
    lead = (
        "The S&P Global US Services PMI rose to 58.8 from 56.5 in August 2026, "
        "final figures for September 2026 showed."
    )
    with pytest.raises(ValueError, match="country/index/month"):
        te.parse_article(html(lead), stream="us_services", url=URL)


def test_current_official_month_beats_later_secondary_final_and_late_flash(tmp_path, monkeypatch):
    series = load_definitions().by_id()["jp.pmi_manufacturing"]
    primary_url = "https://www.pmi.spglobal.com/Public/Home/PressRelease/" + "a" * 32
    primary = SourceDocument(
        source_id=series.source_id,
        request_key=SEP.isoformat(),
        source_url=primary_url,
        content_type="application/pdf",
        fetched_at=datetime(2026, 10, 1, tzinfo=UTC),
        payload=b"49.1",
        metadata={"observed_at": SEP.isoformat(), "release_observed_at": SEP.isoformat()},
    )
    # Exercise selection over retained receipts; real PDF decoding has its own fixture.
    monkeypatch.setattr(
        pmi_reading,
        "extract_pdf_text",
        lambda body: f"The headline PMI posted {body.decode()} in September.",
    )
    newer_secondary = document(html(FINAL, published="2026-10-04T00:35:15Z"))
    flash_url = URL[:-3] + "101"
    late_flash = document(
        html(FLASH, url=flash_url, published="2026-09-24T00:30:00Z"),
        url=flash_url,
        when=WHEN,
    )
    october_url = URL[:-3] + "102"
    october_flash = document(
        html(
            FLASH.replace("September", "October").replace("54.1", "51.3"),
            url=october_url,
            published="2026-10-23T00:30:00Z",
        ),
        url=october_url,
    )
    corrected_primary = replace(
        primary, fetched_at=primary.fetched_at + timedelta(days=1), payload=b"50.0"
    )

    with closing(store.initialize_database(tmp_path / "source.sqlite")) as conn, conn:
        for doc in (newer_secondary, primary, late_flash, corrected_primary, october_flash):
            store_document(conn, doc)
        # Select inside the requested *observation* interval. Later publication or
        # acquisition is not a reason to drop a reported September observation.
        values = read_document_series(conn, series, start=SEP, end=SEP)
        assert [(r.value, r.observed_at, r.source_url, r.vintage_at) for r in values] == [
            (50.0, SEP, primary_url, corrected_primary.fetched_at)
        ]
        assert "secondary_delivery" not in values[0].quality_flags
        october = date(2026, 10, 1)
        values = read_document_series(conn, series, start=october, end=october)
        assert [(r.value, r.observed_at, r.quality_flags) for r in values] == [
            (51.3, october, ("secondary_delivery", "flash"))
        ]


def test_current_secondary_uses_publication_order_not_arrival_when_phase_matches(tmp_path):
    series = load_definitions().by_id()["jp.pmi_manufacturing"]
    old_url = URL[:-3] + "125"
    old_final = document(
        html(FINAL, url=old_url, published="2026-10-01T05:00:00Z"),
        url=old_url,
        when=WHEN,
    )
    revised = document(
        html(FINAL.replace("54.1", "53.7"), published="2026-10-02T05:00:00Z"),
        when=WHEN - timedelta(hours=12),
    )
    with closing(store.initialize_database(tmp_path / "source.sqlite")) as conn, conn:
        for doc in (revised, old_final):
            store_document(conn, doc)
        records = read_document_series(conn, series, start=SEP, end=SEP)
    assert [(r.value, r.source_url, r.vintage_at, r.quality_flags) for r in records] == [
        (53.7, URL, revised.fetched_at, ("secondary_delivery", "final"))
    ]


def test_prefer_official_regardless_of_order_even_if_secondary_body_is_unreadable(monkeypatch):
    series = load_definitions().by_id()["jp.pmi_manufacturing"]
    unreadable = replace(document(html(FINAL)), payload=b"<html>unavailable</html>")
    official_url = "https://www.pmi.spglobal.com/Public/Home/PressRelease/" + "a" * 32
    official = SourceDocument(
        source_id=series.source_id,
        request_key=SEP.isoformat(),
        source_url=official_url,
        content_type="application/pdf",
        fetched_at=WHEN - timedelta(days=7),
        payload=b"%PDF synthetic",
        metadata={"observed_at": SEP.isoformat(), "release_observed_at": SEP.isoformat()},
    )
    monkeypatch.setattr(
        pmi_reading, "extract_pdf_text", lambda _: "The headline PMI posted 0.0 in September."
    )
    for receipts in ((official, unreadable), (unreadable, official)):
        readings = pmi_reading.read_pmi_documents(series, receipts, start=SEP, end=SEP)
        assert [(r.observed_at, r.value, r.source_url) for r in readings] == [
            (SEP, 0.0, official_url)
        ]
    with pytest.raises(ValueError, match="TE PMI main article"):
        pmi_reading.read_pmi_documents(series, (unreadable,), start=SEP, end=SEP)
    assert pmi_reading.read_pmi_documents(series, (), start=SEP, end=SEP) == ()


def test_secondary_cannot_move_between_months_if_retained_period_disagrees():
    series = load_definitions().by_id()["jp.pmi_manufacturing"]
    doc = document(html(FINAL))
    changed = replace(doc, metadata={**doc.metadata, "observed_at": "2026-10-01"})
    with pytest.raises(ValueError, match="bound observation month"):
        pmi_reading.read_pmi_documents(series, (changed,), start=SEP, end=END)
