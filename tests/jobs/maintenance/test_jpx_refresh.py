"""JPX acquisition and explicit offline replay use the same retained source bundle."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.helpers.jpx_earnings import FIXTURES, SOURCE_FILES

from baibai_engine.data_collection.providers.jpx.models import JPXResponse
from baibai_engine.data_collection.providers.jpx.receipts import (
    complete_bundle,
    create_bundle,
    retain_bundle_response,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_decode import (
    resolve_earnings_calendar_urls,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_models import JPXProviderError
from baibai_engine.machine_analysis.observations.disclosures.jpx_reader import (
    read_jpx_regulations,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_rebuild import (
    rebuild_jpx_snapshot,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_sources import (
    JPX_EARNINGS_CALENDAR_INDEX_URL,
    JPX_REGULATION_URLS,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.maintenance.market import cli as app

ASOF = date(2026, 10, 15)


def _copy_source_only(source: Path, target: Path) -> None:
    with sqlite3.connect(source) as original, open_connection(target) as fresh:
        for table in ("jpx_source_bundles", "jpx_source_responses"):
            rows = original.execute(f"SELECT * FROM {table}").fetchall()
            columns = len(original.execute(f"PRAGMA table_info({table})").fetchall())
            fresh.executemany(
                f"INSERT INTO {table} VALUES ({','.join('?' for _ in range(columns))})", rows
            )
        fresh.commit()


def test_earnings_index_filters_external_and_unrelated_links() -> None:
    html = b"""
    <a href="./files/kessan-1.xlsx">one</a>
    <a href="/listing/event-schedules/financial-announcement/files/kessan-2.xls">two</a>
    <a href="https://example.com/listing/event-schedules/financial-announcement/kessan-3.xlsx">external</a>
    <a href="/listing/event-schedules/other/kessan-4.xlsx">other</a>
    <a href="./files/notes.xlsx">notes</a>
    """
    assert resolve_earnings_calendar_urls(html, JPX_EARNINGS_CALENDAR_INDEX_URL) == (
        "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/files/kessan-1.xlsx",
        "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/files/kessan-2.xls",
    )


def test_historical_regulation_urls_can_rebuild_without_current_registry_match(
    tmp_path: Path,
) -> None:
    path = tmp_path / "market.sqlite"
    started = datetime.now(UTC)
    urls = {name: url + "?archive=1" for name, url in JPX_REGULATION_URLS.items()}
    with open_connection(path) as conn:
        bundle_id = create_bundle(conn, "regulations", ASOF, started, tuple(urls.items()))
        for name, url in urls.items():
            retain_bundle_response(
                conn,
                bundle_id,
                name,
                JPXResponse(
                    url, started, "text/html", (FIXTURES / SOURCE_FILES[name]).read_bytes()
                ),
            )
        complete_bundle(conn, bundle_id)
    assert rebuild_jpx_snapshot(path, bundle_id).flags_by_ticker["130A"] == ("特別注意銘柄",)


@pytest.mark.parametrize("invalid", ["missing_source", "unsupported_format"])
def test_regulation_rebuild_rejects_incomplete_policy_or_format(
    tmp_path: Path, invalid: str
) -> None:
    path = tmp_path / "market.sqlite"
    now = datetime.now(UTC)
    sources = dict(JPX_REGULATION_URLS)
    if invalid == "missing_source":
        sources.pop("取引停止")
    else:
        sources["整理銘柄"] = "https://www.jpx.co.jp/listing/market-alerts/supervision/notes"
    with open_connection(path) as conn:
        bundle_id = create_bundle(conn, "regulations", ASOF, now, tuple(sources.items()))
        for name, url in sources.items():
            content_type = (
                "text/plain"
                if invalid == "unsupported_format" and name == "整理銘柄"
                else "text/html"
            )
            retain_bundle_response(
                conn,
                bundle_id,
                name,
                JPXResponse(url, now, content_type, (FIXTURES / SOURCE_FILES[name]).read_bytes()),
            )
        complete_bundle(conn, bundle_id)
    with pytest.raises(
        JPXProviderError, match=r"four required sources|unsupported JPX regulation source format"
    ):
        rebuild_jpx_snapshot(path, bundle_id)
    assert read_jpx_regulations(path, ASOF) is None


def test_calendar_manifest_must_equal_retained_index_links(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    now = datetime.now(UTC)
    wrong = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/kessan-other.xlsx"
    with open_connection(path) as conn:
        bundle_id = create_bundle(
            conn,
            "earnings_calendar",
            ASOF,
            now,
            (("index", JPX_EARNINGS_CALENDAR_INDEX_URL), ("workbook:0", wrong)),
        )
        retain_bundle_response(
            conn,
            bundle_id,
            "index",
            JPXResponse(
                JPX_EARNINGS_CALENDAR_INDEX_URL,
                now,
                "text/html",
                b"<a href='kessan.xlsx'>daily</a>",
            ),
        )
        retain_bundle_response(
            conn,
            bundle_id,
            "workbook:0",
            JPXResponse(wrong, now, "application/octet-stream", b"malformed"),
        )
        complete_bundle(conn, bundle_id)
    with pytest.raises(JPXProviderError, match="manifest differs"):
        rebuild_jpx_snapshot(path, bundle_id)


def test_cli_rebuild_is_offline_and_does_not_create_missing_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "market.sqlite"
    now = datetime.now(UTC)
    with open_connection(path) as conn:
        bundle_id = create_bundle(
            conn, "regulations", ASOF, now, tuple(JPX_REGULATION_URLS.items())
        )
        for name, url in JPX_REGULATION_URLS.items():
            retain_bundle_response(
                conn,
                bundle_id,
                name,
                JPXResponse(url, now, "text/html", (FIXTURES / SOURCE_FILES[name]).read_bytes()),
            )
        complete_bundle(conn, bundle_id)
    replay_path = tmp_path / "replay.sqlite"
    _copy_source_only(path, replay_path)
    with (
        patch.object(app, "load_project_env", side_effect=AssertionError("env")),
        patch("requests.Session", side_effect=AssertionError("session")),
    ):
        assert (
            app.main(
                [
                    "rebuild-jpx-snapshot",
                    "--bundle-id",
                    bundle_id,
                    "--sqlite-path",
                    str(replay_path),
                ]
            )
            == 0
        )
        assert "collection=regulations" in capsys.readouterr().out
        missing = tmp_path / "missing.sqlite"
        assert (
            app.main(
                ["rebuild-jpx-snapshot", "--bundle-id", bundle_id, "--sqlite-path", str(missing)]
            )
            == 1
        )
        assert not missing.exists()
        assert "unavailable" in capsys.readouterr().err
        assert (
            app.main(
                [
                    "rebuild-jpx-snapshot",
                    "--bundle-id",
                    "unknown",
                    "--sqlite-path",
                    str(replay_path),
                ]
            )
            == 1
        )
        assert "not retained" in capsys.readouterr().err
