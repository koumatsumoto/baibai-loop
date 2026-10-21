"""One retained JPX delisting bundle defines the complete observed L2 view."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.helpers.http_doubles import FakeSession, html_response, response
from tests.helpers.l1_release import market_store
from tests.helpers.lake_policy import narrow_release_policy

from baibai_engine.collection.jpx.receipts import read_bundle, read_complete_bundle
from baibai_engine.market.delistings import (
    JPX_DELISTING_INDEX_URL,
    DelistingRecord,
    DelistingSourceError,
    build_delisting_snapshot,
    parse_delisting_page,
    read_jpx_delistings,
    resolve_delisting_archive_urls,
    store_jpx_delistings,
)
from baibai_engine.market.jpx_models import JPXProviderError
from baibai_engine.market.jpx_rebuild import rebuild_jpx_snapshot
from baibai_engine.market.jpx_refresh import JPXRefresh
from baibai_engine.screening.cli import app
from baibai_engine.screening.cli.delisting_cli import refresh_jpx_delistings_command
from baibai_engine.storage.lake.datasets import LAKE_DATASETS
from baibai_engine.storage.lake.duck import lake_session
from baibai_engine.storage.lake.hydrate import hydrate_market_store
from baibai_engine.storage.lake.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.lake.reader import resolve_release
from baibai_engine.storage.lake.release import create_release
from baibai_engine.storage.lake.sources import sha256_file
from baibai_engine.storage.lake.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.observations.lake_origin import LakeStoreOrigin, write_lake_store_origin
from baibai_engine.storage.observations.schema import open_connection

ASOF = date(2026, 10, 15)
ARCHIVE = JPX_DELISTING_INDEX_URL + "archives-01.html"
DATASETS = ("jquants.daily_bars", "jpx.source_bundles", "jpx.source_responses", "jpx.delistings")
HEADER = (
    "<tr><th>上場廃止日</th><th>銘柄名</th><th>コード</th>"
    "<th>市場区分</th><th>上場廃止理由</th></tr>"
)


def _page(*rows: tuple[str, str, str, str, str], links: str = "") -> bytes:
    body = "<table>" + HEADER
    for on, name, ticker, market, reason in rows:
        body += f"<tr><td>{on}</td><td>{name}</td><td>{ticker}</td><td>{market}</td><td>{reason}</td></tr>"
    return (body + "</table>" + links).encode("utf-8")


def _index(*, linked: bool = True, reason: str = "買収") -> bytes:
    return _page(
        ("2026/05/01", "新会社", "2000", "プライム", reason),
        links='<a href="archives-01.html">過去分</a>' if linked else "",
    )


def _archive(*, reason: str = "株式併合") -> bytes:
    return _page(("2024/05/01", "旧会社", "1000", "スタンダード", reason))


def _session(
    index: bytes, archive: bytes | None = None, *, archive_status: int = 200
) -> FakeSession:
    answers = {JPX_DELISTING_INDEX_URL: html_response(index)}
    if archive is not None:
        answers[ARCHIVE] = response(
            archive, status_code=archive_status, content_type="text/html; charset=UTF-8"
        )
    return FakeSession(by_url=answers)


def _bundle_ids(path: Path) -> list[str]:
    with sqlite3.connect(path) as conn:
        return [
            row[0]
            for row in conn.execute("SELECT bundle_id FROM jpx_source_bundles ORDER BY rowid")
        ]


def _copy_raw_only(source: Path, target: Path) -> None:
    with sqlite3.connect(source) as src, open_connection(target) as dst:
        for table in ("jpx_source_bundles", "jpx_source_responses"):
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            count = len(src.execute(f"PRAGMA table_info({table})").fetchall())
            dst.executemany(
                f"INSERT INTO {table} VALUES ({','.join('?' for _ in range(count))})", rows
            )
        dst.commit()


def _record(
    ticker: str, *, on: date = date(2024, 5, 1), reason: str = "株式併合"
) -> DelistingRecord:
    return DelistingRecord(
        delisted_on=on, ticker=ticker, name="旧会社", market="スタンダード", reason=reason
    )


def test_link_resolution_rejects_external_query_and_guessed_archives() -> None:
    index = _index() + (
        b'<a href="https://evil.example/listing/stocks/delisted/archives-02.html">x</a>'
        b'<a href="https://www.jpx.co.jp.evil.example/listing/stocks/delisted/archives-03.html">x</a>'
        b'<a href="//evil.example/listing/stocks/delisted/archives-04.html">x</a>'
        b'<a href="http://www.jpx.co.jp/listing/stocks/delisted/archives-05.html">x</a>'
        b'<a href="archives-06.html?to=evil">x</a>'
        b'<option value="archives-07.html">x</option>'
    )
    assert resolve_delisting_archive_urls(index, JPX_DELISTING_INDEX_URL) == (
        ARCHIVE,
        JPX_DELISTING_INDEX_URL + "archives-07.html",
    )
    with pytest.raises(DelistingSourceError, match="allowed origin"):
        resolve_delisting_archive_urls(index, "https://evil.example/index.html")


@pytest.mark.parametrize(
    "body",
    [
        b"<html><title>error</title></html>",
        b"<table><tr><th>other</th></tr></table>",
    ],
)
def test_http_200_without_target_table_is_not_empty_history(body: bytes) -> None:
    with pytest.raises(DelistingSourceError, match="no recognizable table"):
        parse_delisting_page(body, JPX_DELISTING_INDEX_URL)


@pytest.mark.parametrize(
    "cells",
    [
        ("2026/05/01", "会社", "20000", "プライム", "買収"),
        ("2026/05/01", "", "2000", "プライム", "買収"),
        ("2026/05/01", "会社", "2000", "プライム", ""),
        ("2026/13/01", "会社", "2000", "プライム", "買収"),
        ("2026年5月1日", "会社", "2000", "プライム", "買収"),
    ],
)
def test_recognizable_date_with_invalid_code_cells_or_date_fails(cells: tuple[str, ...]) -> None:
    with pytest.raises(DelistingSourceError):
        parse_delisting_page(_page(cells), JPX_DELISTING_INDEX_URL)
    incomplete = ("2026/05/01", "会社", "2000", "プライム")
    body = (
        "<table>" + HEADER + "<tr>" + "".join(f"<td>{v}</td>" for v in incomplete) + "</tr></table>"
    ).encode()
    with pytest.raises(DelistingSourceError, match="incomplete"):
        parse_delisting_page(body, JPX_DELISTING_INDEX_URL)


def test_nested_table_keeps_outer_cell_text_without_adding_a_row() -> None:
    body = (
        "<table>"
        + HEADER
        + "<tr><td>2026/05/01</td><td><span>新</span>会社</td><td>2000</td>"
        + "<td>プライム</td><td>買<table><tr><td>2025/01/01</td><td>補足</td>"
        + "<td>3000</td><td>市場</td><td>説明</td></tr></table>収</td></tr></table>"
    ).encode()
    rows = parse_delisting_page(body, JPX_DELISTING_INDEX_URL)
    assert len(rows) == 1
    assert rows[0].name == "新会社"
    assert rows[0].reason == "買2025/01/01補足3000市場説明収"


def test_equal_duplicate_deduplicates_but_conflict_fails() -> None:
    row = ("2024/05/01", "旧会社", "1000", "スタンダード", "株式併合")
    assert (
        len(
            build_delisting_snapshot(((JPX_DELISTING_INDEX_URL, _page(row)), (ARCHIVE, _page(row))))
        )
        == 1
    )
    with pytest.raises(DelistingSourceError, match="conflicting"):
        build_delisting_snapshot(
            ((JPX_DELISTING_INDEX_URL, _page(row)), (ARCHIVE, _archive(reason="別理由")))
        )


def test_refresh_replaces_observed_pages_and_old_bundle_can_be_replayed(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    first_session = _session(_index(), _archive())
    first = JPXRefresh(sqlite_path=path, session=first_session).refresh_delistings_snapshot(ASOF)
    assert first_session.calls == [JPX_DELISTING_INDEX_URL, ARCHIVE]
    assert [row.ticker for row in first] == ["1000", "2000"]
    with sqlite3.connect(path) as conn:
        first_bundle = read_complete_bundle(conn, _bundle_ids(path)[0])
    assert first_bundle.expected_resources == (
        ("index", JPX_DELISTING_INDEX_URL),
        ("page:0", ARCHIVE),
    )
    assert {receipt.resource_name: receipt.response.body for receipt in first_bundle.responses} == {
        "index": _index(),
        "page:0": _archive(),
    }
    assert all(
        receipt.response.content_type == "text/html; charset=UTF-8"
        for receipt in first_bundle.responses
    )

    corrected = _index(linked=False, reason="訂正買収")
    second = JPXRefresh(sqlite_path=path, session=_session(corrected)).refresh_delistings_snapshot(
        ASOF
    )
    assert [(row.ticker, row.reason) for row in second] == [("2000", "訂正買収")]
    assert read_jpx_delistings(path) == second
    with sqlite3.connect(path) as conn:
        second_bundle = read_complete_bundle(conn, _bundle_ids(path)[1])
    assert second_bundle.expected_resources == (("index", JPX_DELISTING_INDEX_URL),)

    clean = tmp_path / "clean.sqlite"
    _copy_raw_only(path, clean)
    assert rebuild_jpx_snapshot(clean, second_bundle.bundle_id) == second
    assert read_jpx_delistings(clean) == second
    assert rebuild_jpx_snapshot(path, second_bundle.bundle_id) == second
    assert read_jpx_delistings(path) == second
    assert rebuild_jpx_snapshot(clean, first_bundle.bundle_id) == first
    assert read_jpx_delistings(clean) == first
    assert rebuild_jpx_snapshot(clean, second_bundle.bundle_id) == second
    assert read_jpx_delistings(clean) == second


def test_archive_http_failure_and_malformed_200_keep_previous_view(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    old = JPXRefresh(
        sqlite_path=path, session=_session(_index(), _archive())
    ).refresh_delistings_snapshot(ASOF)
    failing = _session(_index(), b"unavailable", archive_status=503)
    with pytest.raises(JPXProviderError, match="HTTP 503"):
        JPXRefresh(sqlite_path=path, session=failing).refresh_delistings_snapshot(ASOF)
    with sqlite3.connect(path) as conn:
        partial = read_bundle(conn, _bundle_ids(path)[1])
    assert partial.completed_at is None
    assert partial.expected_resources == (("index", JPX_DELISTING_INDEX_URL), ("page:0", ARCHIVE))
    assert [(r.resource_name, r.response.body) for r in partial.responses] == [("index", _index())]
    assert read_jpx_delistings(path) == old

    malformed = b"<html><title>error</title></html>"
    with pytest.raises(DelistingSourceError, match="no recognizable table"):
        JPXRefresh(
            sqlite_path=path, session=_session(_index(), malformed)
        ).refresh_delistings_snapshot(ASOF)
    with sqlite3.connect(path) as conn:
        completed = read_complete_bundle(conn, _bundle_ids(path)[2])
    assert (
        next(r.response.body for r in completed.responses if r.resource_name == "page:0")
        == malformed
    )
    assert read_jpx_delistings(path) == old


def test_malformed_date_in_target_table_keeps_previous_view(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    old = JPXRefresh(
        sqlite_path=path, session=_session(_index(), _archive())
    ).refresh_delistings_snapshot(ASOF)
    malformed = _page(
        ("2026/05/01", "新会社", "2000", "プライム", "買収"),
        ("2026/05/XX", "別会社", "3000", "スタンダード", "統合"),
    )
    with pytest.raises(DelistingSourceError, match="invalid JPX delisting date"):
        JPXRefresh(sqlite_path=path, session=_session(malformed)).refresh_delistings_snapshot(ASOF)
    assert read_jpx_delistings(path) == old


def test_retained_manifest_must_match_index_links(tmp_path: Path) -> None:
    from baibai_engine.collection.jpx.models import JPXResponse
    from baibai_engine.collection.jpx.receipts import (
        complete_bundle,
        create_bundle,
        retain_bundle_response,
    )

    path = tmp_path / "market.sqlite"
    now = datetime.now(UTC)
    with open_connection(path) as conn:
        bundle_id = create_bundle(
            conn, "delistings", ASOF, now, (("index", JPX_DELISTING_INDEX_URL),)
        )
        retain_bundle_response(
            conn,
            bundle_id,
            "index",
            JPXResponse(JPX_DELISTING_INDEX_URL, now, "text/html", _index()),
        )
        complete_bundle(conn, bundle_id)
    with pytest.raises(DelistingSourceError, match="manifest differs"):
        rebuild_jpx_snapshot(path, bundle_id)
    assert read_jpx_delistings(path) == ()


def test_store_replaces_and_rolls_back_duplicate_or_sql_failure(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    old = _record("1000")
    store_jpx_delistings(path, (old,))
    newer = _record("2000", on=date(2026, 5, 1))
    with pytest.raises(sqlite3.IntegrityError):
        store_jpx_delistings(path, (newer, newer))
    assert read_jpx_delistings(path) == (old,)
    with open_connection(path) as conn:
        conn.execute(
            "CREATE TRIGGER stop_delisting_insert BEFORE INSERT ON jpx_delistings BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
        conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        store_jpx_delistings(path, (newer,))
    assert read_jpx_delistings(path) == (old,)
    with pytest.raises(DelistingSourceError, match="zero"):
        store_jpx_delistings(path, ())


def test_rebuild_cli_and_reader_are_offline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "market.sqlite"
    JPXRefresh(
        sqlite_path=path, session=_session(_index(), _archive())
    ).refresh_delistings_snapshot(ASOF)
    bundle_id = _bundle_ids(path)[0]
    clean = tmp_path / "clean.sqlite"
    _copy_raw_only(path, clean)
    with (
        patch.object(app, "load_project_env", side_effect=AssertionError("env")),
        patch("requests.Session", side_effect=AssertionError("HTTP")),
    ):
        assert (
            app.main(
                ["rebuild-jpx-snapshot", "--bundle-id", bundle_id, "--sqlite-path", str(clean)]
            )
            == 0
        )
        assert "collection=delistings" in capsys.readouterr().out
        assert read_jpx_delistings(clean) == read_jpx_delistings(path)


def test_refresh_cli_routes_to_jpx_refresh_and_closes(tmp_path: Path) -> None:
    with patch("baibai_engine.screening.cli.delisting_cli.JPXRefresh") as factory:
        instance = factory.return_value
        instance.refresh_delistings_snapshot.return_value = (_record("1000"),)
        assert refresh_jpx_delistings_command(sqlite_path=tmp_path / "market.sqlite") == 0
        factory.assert_called_once_with(sqlite_path=tmp_path / "market.sqlite")
        (asof,) = instance.refresh_delistings_snapshot.call_args.args
        assert isinstance(asof, date)
        instance.close.assert_called_once_with()


def test_fixed_release_roundtrip_and_rebuild_from_retained_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    narrow_release_policy(monkeypatch, datasets=DATASETS, carries_history=None)
    source_path = market_store(tmp_path / "source.sqlite")
    JPXRefresh(
        sqlite_path=source_path, session=_session(_index(), _archive())
    ).refresh_delistings_snapshot(ASOF)
    body = _index(linked=False)
    JPXRefresh(sqlite_path=source_path, session=_session(body)).refresh_delistings_snapshot(ASOF)
    bundle_id = _bundle_ids(source_path)[1]
    expected = read_jpx_delistings(source_path)
    assert [row.ticker for row in expected] == ["2000"]
    dataset = LAKE_DATASETS["jpx.delistings"]
    assert dataset.layer == "l2_processed"
    assert dataset.contract_version == 2
    mirror = tmp_path / "mirror"
    built = datetime(2026, 10, 16, tzinfo=UTC)
    with sealed_sqlite_snapshot(sqlite_path=source_path, mirror_root=mirror) as snapshot:
        manifests = [
            export_dataset_snapshot(
                dataset_name=name,
                mirror_root=mirror,
                source_snapshot=snapshot,
                producer_git_commit="a" * 40,
                build_id=f"delist-{name.replace('.', '-')}",
                created_at=built,
            ).manifest_path
            for name in DATASETS
        ]
    manifest_path, manifest = create_release(
        dataset_manifest_paths=manifests,
        mirror_root=mirror,
        release_id="delistings-fixed",
        created_at=built,
    )
    assert manifest.datasets["jpx.delistings"].contract_version == 2
    source = LocalMirrorSource(mirror)
    release = resolve_release(
        source, manifest.release_id, manifest_sha256=sha256_file(manifest_path)
    )
    destination = tmp_path / "hydrated.sqlite"
    shutil.copyfile(source_path, destination)
    with open_connection(destination) as conn:
        for table in ("jpx_source_responses", "jpx_source_bundles", "jpx_delistings"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute(
            "INSERT INTO jpx_delistings(delisted_on, ticker, name, market, reason) "
            "VALUES ('2024-05-01', '1000', '旧会社', 'スタンダード', '株式併合')"
        )
        write_lake_store_origin(conn, LakeStoreOrigin(release.release_id, release.manifest_sha256))
        conn.commit()
    cache = LakeObjectCache(root=tmp_path / "cache", source=source)
    with lake_session() as lake:
        report = hydrate_market_store(
            lake, release=release, cache=cache, store=destination, dataset_names=DATASETS
        )
    assert report.rows["jpx.delistings"] == 1
    assert read_jpx_delistings(destination) == expected
    with sqlite3.connect(destination) as conn:
        retained = read_complete_bundle(conn, bundle_id)
    assert retained.responses[0].response.body == body
    with open_connection(destination) as conn:
        conn.execute("DELETE FROM jpx_delistings")
        conn.commit()
    rebuild_jpx_snapshot(destination, bundle_id)
    assert read_jpx_delistings(destination) == expected
