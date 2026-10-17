"""JPX acquisition and explicit offline replay use the same retained source bundle."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import pytest
from tests.helpers.http_doubles import FakeSession, html_response, response

from baibai_engine.collection.jpx.models import JPXResponse
from baibai_engine.collection.jpx.receipts import (
    complete_bundle,
    create_bundle,
    read_bundle,
    read_complete_bundle,
    retain_bundle_response,
)
from baibai_engine.market.jpx_decode import resolve_earnings_calendar_urls
from baibai_engine.market.jpx_models import JPXProviderError
from baibai_engine.market.jpx_reader import JPXReader
from baibai_engine.market.jpx_rebuild import rebuild_jpx_snapshot
from baibai_engine.market.jpx_refresh import JPXRefresh
from baibai_engine.market.jpx_sources import (
    JPX_EARNINGS_CALENDAR_INDEX_URL,
    JPX_REGULATION_URLS,
)
from baibai_engine.screening.cli import app
from baibai_engine.storage.observations.ingest.jpx import store_jpx_regulations
from baibai_engine.storage.observations.reader import read_jpx_regulations
from baibai_engine.storage.observations.schema import open_connection

ASOF = date(2026, 10, 15)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "jpx"
SOURCE_FILES = {
    "特別注意銘柄": "special_caution.html",
    "整理銘柄": "reorganization.html",
    "取引停止": "trading_halt_empty.html",
    "上場廃止警告": "delisting_warning.html",
}
FILE_A = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/kessan.xlsx"
FILE_B = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/kessan09_1015.xlsx"


def _regulation_session(*, broken: str | None = None) -> FakeSession:
    answers = {
        url: html_response(
            b"not a JPX table" if name == broken else (FIXTURES / SOURCE_FILES[name]).read_bytes()
        )
        for name, url in JPX_REGULATION_URLS.items()
    }
    return FakeSession(by_url=answers)


def _workbook(stamp: str, rows: list[list[str]]) -> bytes:
    buffer = BytesIO()
    pd.DataFrame([[stamp, ""], ["決算発表予定日", "コード"], *rows]).to_excel(
        buffer, index=False, header=False
    )
    return buffer.getvalue()


def _calendar_session(*, bad_workbook: bool = False, bad_index: bool = False) -> FakeSession:
    index = b"<a href='kessan.xlsx'>daily</a><a href='kessan09_1015.xlsx'>archive</a>"
    answers = {
        JPX_EARNINGS_CALENDAR_INDEX_URL: html_response(b"no workbooks" if bad_index else index),
        FILE_A: response(
            _workbook("", [["2026-10-20", "130A"]]),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        FILE_B: response(
            b"malformed xlsx"
            if bad_workbook
            else _workbook("2026年10月14日現在", [["2026-10-21", "130A"], ["2026-10-22", "7203"]]),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    }
    return FakeSession(by_url=answers)


def _bundle_ids(path: Path) -> list[str]:
    with sqlite3.connect(path) as conn:
        return [
            row[0]
            for row in conn.execute("SELECT bundle_id FROM jpx_source_bundles ORDER BY rowid")
        ]


def _copy_source_only(source: Path, target: Path) -> None:
    with sqlite3.connect(source) as original, open_connection(target) as fresh:
        for table in ("jpx_source_bundles", "jpx_source_responses"):
            rows = original.execute(f"SELECT * FROM {table}").fetchall()
            columns = len(original.execute(f"PRAGMA table_info({table})").fetchall())
            fresh.executemany(
                f"INSERT INTO {table} VALUES ({','.join('?' for _ in range(columns))})",
                rows,
            )
        fresh.commit()


def _fetched_at(path: Path, source: str) -> str:
    with sqlite3.connect(path) as conn:
        return str(
            conn.execute(
                "SELECT fetched_at_utc FROM source_coverage WHERE source = ? LIMIT 1", (source,)
            ).fetchone()[0]
        )


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


def test_regulation_refresh_retains_fixed_four_and_replays_into_source_only_store(
    tmp_path: Path,
) -> None:
    path = tmp_path / "market.sqlite"
    session = _regulation_session()
    snapshot = JPXRefresh(sqlite_path=path, session=session).refresh_regulation_snapshot(ASOF)
    assert session.calls == list(JPX_REGULATION_URLS.values())
    assert snapshot.flags_by_ticker["130A"] == ("特別注意銘柄",)
    assert "取引停止" in snapshot.source_names  # genuine empty page is still covered
    assert JPXReader(path).get_regulation_snapshot(ASOF) == snapshot
    with sqlite3.connect(path) as conn:
        bundle = read_complete_bundle(conn, _bundle_ids(path)[0])
    assert bundle.expected_resources == tuple(JPX_REGULATION_URLS.items())
    assert bundle.completed_at is not None
    assert _fetched_at(path, "jpx_regulation_flags") == bundle.completed_at.isoformat(
        timespec="microseconds"
    )
    for receipt in bundle.responses:
        assert receipt.response.content_type == "text/html; charset=UTF-8"
        assert (
            receipt.response.body == (FIXTURES / SOURCE_FILES[receipt.resource_name]).read_bytes()
        )

    replay_path = tmp_path / "replay.sqlite"
    _copy_source_only(path, replay_path)
    assert read_jpx_regulations(replay_path, ASOF) is None
    assert rebuild_jpx_snapshot(replay_path, bundle.bundle_id) == snapshot
    before = _fetched_at(replay_path, "jpx_regulation_flags")
    assert rebuild_jpx_snapshot(replay_path, bundle.bundle_id) == snapshot
    assert _fetched_at(replay_path, "jpx_regulation_flags") == before


def test_regulation_refresh_ignores_existing_l2_and_fails_without_touching_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "market.sqlite"
    store_jpx_regulations(
        path, ASOF, flags_by_ticker={"9999": ("取引停止",)}, source_names=("取引停止",)
    )
    original = read_jpx_regulations(path, ASOF)
    session = _regulation_session(broken="上場廃止警告")
    with pytest.raises(JPXProviderError):
        JPXRefresh(sqlite_path=path, session=session).refresh_regulation_snapshot(ASOF)
    assert session.calls == list(JPX_REGULATION_URLS.values())
    with sqlite3.connect(path) as conn:
        bundle = read_complete_bundle(conn, _bundle_ids(path)[0])
    assert len(bundle.responses) == 4
    assert read_jpx_regulations(path, ASOF) == original


def test_regulation_partial_http_failure_retains_only_prior_responses(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    session = _regulation_session()
    second = list(JPX_REGULATION_URLS.values())[1]
    session._by_url[second] = response(status_code=503)
    with pytest.raises(JPXProviderError, match="HTTP 503"):
        JPXRefresh(sqlite_path=path, session=session).refresh_regulation_snapshot(ASOF)
    with sqlite3.connect(path) as conn:
        bundle = read_bundle(conn, _bundle_ids(path)[0])
    assert bundle.completed_at is None
    assert bundle.expected_resources == tuple(JPX_REGULATION_URLS.items())
    assert [r.resource_name for r in bundle.responses] == [next(iter(JPX_REGULATION_URLS))]
    assert read_jpx_regulations(path, ASOF) is None


def test_calendar_refresh_replays_precedence_and_reader_stays_offline(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    session = _calendar_session()
    snapshot = JPXRefresh(sqlite_path=path, session=session).refresh_earnings_calendar_snapshot(
        ASOF
    )
    assert session.calls == [JPX_EARNINGS_CALENDAR_INDEX_URL, FILE_A, FILE_B]
    assert [(r.ticker, r.announcement_date) for r in snapshot.entries] == [
        ("130A", date(2026, 10, 20)),
        ("7203", date(2026, 10, 22)),
    ]
    assert snapshot.superseded_record_count == 1
    with sqlite3.connect(path) as conn:
        bundle = read_complete_bundle(conn, _bundle_ids(path)[0])
    assert bundle.expected_resources == (
        ("index", JPX_EARNINGS_CALENDAR_INDEX_URL),
        ("workbook:0", FILE_A),
        ("workbook:1", FILE_B),
    )
    assert bundle.completed_at is not None
    assert _fetched_at(path, "jpx_earnings_calendar") == bundle.completed_at.isoformat(
        timespec="microseconds"
    )
    for receipt in bundle.responses:
        original = session._by_url[receipt.response.requested_url]
        assert receipt.response.body == original.content
        assert receipt.response.content_type == original.headers["content-type"]
    assert JPXReader(path).get_earnings_calendar_snapshot(ASOF).entries == snapshot.entries

    replay_path = tmp_path / "replay.sqlite"
    _copy_source_only(path, replay_path)
    with patch("requests.Session", side_effect=AssertionError("network")):
        assert rebuild_jpx_snapshot(replay_path, bundle.bundle_id) == snapshot
        assert rebuild_jpx_snapshot(replay_path, bundle.bundle_id) == snapshot
    assert _fetched_at(replay_path, "jpx_earnings_calendar") == bundle.completed_at.isoformat(
        timespec="microseconds"
    )


@pytest.mark.parametrize("failure", ["index", "workbook"])
def test_calendar_partial_failure_keeps_incomplete_bundle(tmp_path: Path, failure: str) -> None:
    path = tmp_path / "market.sqlite"
    session = _calendar_session(bad_index=failure == "index")
    if failure == "workbook":
        session._by_url[FILE_B] = response(status_code=503)
    with pytest.raises(JPXProviderError):
        JPXRefresh(sqlite_path=path, session=session).refresh_earnings_calendar_snapshot(ASOF)
    with sqlite3.connect(path) as conn:
        bundle = read_bundle(conn, _bundle_ids(path)[0])
    assert bundle.completed_at is None
    assert bundle.responses[0].resource_name == "index"
    assert len(bundle.responses) == (1 if failure == "index" else 2)
    assert (
        bundle.expected_resources is None
        if failure == "index"
        else bundle.expected_resources is not None
    )


def test_calendar_malformed_http_200_completes_l1_without_l2(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    JPXRefresh(sqlite_path=path, session=_calendar_session()).refresh_earnings_calendar_snapshot(
        ASOF
    )
    original_rows = JPXReader(path).get_earnings_calendar_snapshot(ASOF).entries
    original_fetched_at = _fetched_at(path, "jpx_earnings_calendar")
    session = _calendar_session(bad_workbook=True)
    with pytest.raises(JPXProviderError, match="failed to parse JPX earnings"):
        JPXRefresh(sqlite_path=path, session=session).refresh_earnings_calendar_snapshot(ASOF)
    with sqlite3.connect(path) as conn:
        bundle = read_complete_bundle(conn, _bundle_ids(path)[1])
    assert (
        next(r.response.body for r in bundle.responses if r.resource_name == "workbook:1")
        == b"malformed xlsx"
    )
    assert JPXReader(path).get_earnings_calendar_snapshot(ASOF).entries == original_rows
    assert _fetched_at(path, "jpx_earnings_calendar") == original_fetched_at


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


def test_bootstrap_creates_new_bundles_for_each_attempt(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    session = FakeSession(by_url={**_calendar_session()._by_url, **_regulation_session()._by_url})
    refresh = JPXRefresh(sqlite_path=path, session=session)
    first = refresh.bootstrap_cache(ASOF)
    second = refresh.bootstrap_cache(ASOF)
    assert first == second
    assert len(_bundle_ids(path)) == 4
    assert (
        session.calls
        == [JPX_EARNINGS_CALENDAR_INDEX_URL, FILE_A, FILE_B, *JPX_REGULATION_URLS.values()] * 2
    )


def test_cli_rebuild_is_offline_and_does_not_create_missing_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "market.sqlite"
    JPXRefresh(sqlite_path=path, session=_regulation_session()).refresh_regulation_snapshot(ASOF)
    bundle_id = _bundle_ids(path)[0]
    replay_path = tmp_path / "replay.sqlite"
    _copy_source_only(path, replay_path)
    with (
        patch.object(app, "load_project_env", side_effect=AssertionError("env")),
        patch.object(app, "JPXRefresh", side_effect=AssertionError("refresh")),
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


def test_refresh_closes_only_its_owned_lazy_session(tmp_path: Path) -> None:
    owned = Mock()
    owned.get.return_value.status_code = 503
    with patch("baibai_engine.collection.jpx.http.requests.Session", return_value=owned) as factory:
        refresh = JPXRefresh(sqlite_path=tmp_path / "owned.sqlite")
        refresh.close()
        factory.assert_not_called()
        with pytest.raises(JPXProviderError, match="HTTP 503"):
            refresh.refresh_regulation_snapshot(ASOF)
        factory.assert_called_once_with()
        refresh.close()
        owned.close.assert_called_once_with()
    injected = Mock()
    JPXRefresh(sqlite_path=tmp_path / "injected.sqlite", session=injected).close()
    injected.close.assert_not_called()
