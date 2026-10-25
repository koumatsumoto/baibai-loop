"""Capital-policy workbook snapshots are explicit raw bundles and replace one L2 view."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import openpyxl
import pytest
from tests.helpers.http_doubles import FakeSession, response
from tests.helpers.l1_release import market_store
from tests.helpers.lake_policy import narrow_release_policy

from baibai_engine.data_collection.jpx.models import JPXResponse
from baibai_engine.data_collection.jpx.receipts import (
    complete_bundle,
    create_bundle,
    read_bundle,
    read_complete_bundle,
    retain_bundle_response,
)
from baibai_engine.market.jpx_models import JPXProviderError
from baibai_engine.market.jpx_rebuild import rebuild_jpx_snapshot
from baibai_engine.market.jpx_refresh import JPXRefresh
from baibai_engine.market.valuation_catalysts import (
    TSE_CAPITAL_POLICY_URL,
    ValuationCatalystError,
    parse_tse_capital_policy_workbook,
    read_valuation_catalyst_contexts,
    store_tse_capital_policy_rows,
)
from baibai_engine.source_maintenance import cli as app
from baibai_engine.source_maintenance.valuation_catalyst_cli import (
    refresh_tse_capital_policy_command,
)
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
DATASETS = (
    "jquants.daily_bars",
    "jpx.source_bundles",
    "jpx.source_responses",
    "tse.capital_policy_snapshots",
)
GROUP = (
    None,
    "業種\nコード",
    "業種",
    "市場区分",
    "証券\nコード",
    "銘柄名",
    "開示状況",
    None,
    "開示内容の\nアップデート日",
    "開示内容 ※１",
    "機関投資家からのより活発なコンタクトを希望 ※２",
    None,
    None,
    "英文開示",
)
DETAIL = (
    None,
    None,
    None,
    None,
    None,
    None,
    "要請に基づく\n開示状況",
    "前月からの\n開示状況の変更",
    None,
    None,
    "申請状況",
    "掲載開始日",
    "コンタクト先",
    None,
)


def _workbook(sheets: list[tuple[int, int, list[tuple[int, str, date | None]]]]) -> bytes:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for year, month, rows in sheets:
        worksheet = workbook.create_sheet(f"開示企業一覧（{year}年{month}月末時点）")
        worksheet.append(("本資料の目的",))
        worksheet.append(GROUP)
        worksheet.append(DETAIL)
        for ticker, status, updated in rows:
            worksheet.append(
                (
                    None,
                    50,
                    "水産・農林業",
                    "プライム",
                    ticker,
                    "企業",
                    status,
                    "",
                    updated,
                    "本文",
                    "",
                    "",
                    "",
                    "有",
                )
            )
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def _initial_workbook() -> bytes:
    return _workbook(
        [
            (2026, 5, [(1301, "開示済", date(2026, 5, 20))]),
            (2026, 6, [(1301, "開示済", date(2026, 6, 23)), (1332, "検討中", None)]),
        ]
    )


def _corrected_workbook() -> bytes:
    return _workbook(
        [
            (2026, 6, [(1301, "検討中", None)]),
            (2026, 7, [(1301, "開示済", date(2026, 7, 15)), (1332, "検討中", None)]),
        ]
    )


def _session(body: bytes, *, status: int = 200) -> FakeSession:
    return FakeSession(
        by_url={
            TSE_CAPITAL_POLICY_URL: response(
                body,
                status_code=status,
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        }
    )


def _rows(path: Path) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(path) as conn:
        return tuple(
            conn.execute(
                "SELECT * FROM tse_capital_policy_snapshots ORDER BY snapshot_month_end, ticker"
            ).fetchall()
        )


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


def test_refresh_retains_bytes_and_rebuild_replaces_old_months(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    original = _initial_workbook()
    corrected = _corrected_workbook()
    first = JPXRefresh(
        sqlite_path=path, session=_session(original)
    ).refresh_capital_policy_snapshot(ASOF)
    assert first.sheet_count == 2
    assert first.row_count == 3
    assert [row[0] for row in _rows(path)] == ["2026-05-31", "2026-06-30", "2026-06-30"]
    session = _session(corrected)
    second = JPXRefresh(sqlite_path=path, session=session).refresh_capital_policy_snapshot(ASOF)
    assert session.calls == [TSE_CAPITAL_POLICY_URL]
    assert second.sheet_count == 2
    assert second.row_count == 3
    assert [row[0] for row in _rows(path)] == ["2026-06-30", "2026-07-31", "2026-07-31"]
    corrected_rows = _rows(path)
    june = next(row for row in corrected_rows if row[0] == "2026-06-30")
    july = next(row for row in corrected_rows if row[0] == "2026-07-31" and row[1] == "1301")
    assert june[2] == "considering"
    assert june[6] is None
    assert july[2] == "disclosed"
    assert july[6] == "2026-07-31"
    assert july[7] == 0

    with sqlite3.connect(path) as conn:
        bundle = read_complete_bundle(conn, _bundle_ids(path)[1])
    assert bundle.collection_name == "capital_policy"
    assert bundle.asof_date == ASOF
    assert bundle.expected_resources == (("workbook", TSE_CAPITAL_POLICY_URL),)
    assert bundle.responses[0].response.body == corrected
    assert bundle.responses[0].response.content_type.startswith("application/vnd.")
    clean = tmp_path / "clean.sqlite"
    _copy_raw_only(path, clean)
    assert rebuild_jpx_snapshot(clean, bundle.bundle_id) == second
    assert _rows(clean) == corrected_rows
    assert rebuild_jpx_snapshot(path, bundle.bundle_id) == second
    assert _rows(path) == corrected_rows


def test_http_failure_or_malformed_200_preserves_old_l2(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    JPXRefresh(
        sqlite_path=path, session=_session(_initial_workbook())
    ).refresh_capital_policy_snapshot(ASOF)
    old = _rows(path)
    bad_http = _session(b"unavailable", status=503)
    with pytest.raises(JPXProviderError, match="HTTP 503"):
        JPXRefresh(sqlite_path=path, session=bad_http).refresh_capital_policy_snapshot(ASOF)
    with sqlite3.connect(path) as conn:
        partial = read_bundle(conn, _bundle_ids(path)[1])
    assert partial.expected_resources == (("workbook", TSE_CAPITAL_POLICY_URL),)
    assert partial.completed_at is None
    assert partial.responses == ()
    assert _rows(path) == old

    malformed = b"malformed workbook bytes\x00\xff"
    bad_body = _session(malformed)
    with pytest.raises(ValuationCatalystError, match="failed to parse"):
        JPXRefresh(sqlite_path=path, session=bad_body).refresh_capital_policy_snapshot(ASOF)
    with sqlite3.connect(path) as conn:
        complete = read_complete_bundle(conn, _bundle_ids(path)[2])
    assert complete.responses[0].response.body == malformed
    assert _rows(path) == old


def test_rebuild_rejects_bundle_with_extra_resource_without_changing_l2(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    old_rows = parse_tse_capital_policy_workbook(_initial_workbook())
    store_tse_capital_policy_rows(path, old_rows)
    original = _rows(path)
    captured = datetime.now(UTC)
    other = TSE_CAPITAL_POLICY_URL + "?other=1"
    with open_connection(path) as conn:
        bundle_id = create_bundle(
            conn,
            "capital_policy",
            ASOF,
            captured,
            (("workbook", TSE_CAPITAL_POLICY_URL), ("extra", other)),
        )
        for name, url in (("workbook", TSE_CAPITAL_POLICY_URL), ("extra", other)):
            retain_bundle_response(
                conn,
                bundle_id,
                name,
                JPXResponse(url, captured, "application/vnd.ms-excel", _corrected_workbook()),
            )
        complete_bundle(conn, bundle_id)
    with pytest.raises(JPXProviderError, match="one workbook"):
        rebuild_jpx_snapshot(path, bundle_id)
    assert _rows(path) == original


def test_duplicate_and_sql_insert_failure_roll_back_whole_replacement(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    old_rows = parse_tse_capital_policy_workbook(_initial_workbook())
    store_tse_capital_policy_rows(path, old_rows)
    before = _rows(path)
    corrected = parse_tse_capital_policy_workbook(_corrected_workbook())
    with pytest.raises(sqlite3.IntegrityError):
        store_tse_capital_policy_rows(path, (*corrected, corrected[0]))
    assert _rows(path) == before
    with open_connection(path) as conn:
        conn.execute(
            "CREATE TRIGGER stop_policy_insert BEFORE INSERT ON tse_capital_policy_snapshots "
            "BEGIN SELECT RAISE(ABORT, 'test insert failure'); END"
        )
        conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="test insert failure"):
        store_tse_capital_policy_rows(path, corrected)
    assert _rows(path) == before


def test_rebuild_cli_and_normal_reader_never_acquire(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "market.sqlite"
    JPXRefresh(
        sqlite_path=path, session=_session(_corrected_workbook())
    ).refresh_capital_policy_snapshot(ASOF)
    bundle_id = _bundle_ids(path)[0]
    clean = tmp_path / "clean.sqlite"
    _copy_raw_only(path, clean)
    with (
        patch.object(app, "load_project_env", side_effect=AssertionError("env")),
        patch("requests.Session", side_effect=AssertionError("HTTP session")),
    ):
        assert (
            app.main(
                ["rebuild-jpx-snapshot", "--bundle-id", bundle_id, "--sqlite-path", str(clean)]
            )
            == 0
        )
        assert "collection=capital_policy" in capsys.readouterr().out
        assert _rows(clean) == _rows(path)
        context = read_valuation_catalyst_contexts(clean, as_of=date(2026, 7, 31), tickers=["1301"])
        assert context["1301"].tse_capital_policy_status == "disclosed"


def test_explicit_refresh_cli_uses_jpx_refresh_and_closes_it(tmp_path: Path) -> None:
    with patch("baibai_engine.source_maintenance.valuation_catalyst_cli.JPXRefresh") as fake:
        instance = fake.return_value
        instance.refresh_capital_policy_snapshot.return_value.sheet_count = 2
        instance.refresh_capital_policy_snapshot.return_value.row_count = 3
        instance.refresh_capital_policy_snapshot.return_value.disclosed_count = 1
        instance.refresh_capital_policy_snapshot.return_value.considering_count = 2
        assert refresh_tse_capital_policy_command(sqlite_path=tmp_path / "market.sqlite") == 0
        fake.assert_called_once_with(sqlite_path=tmp_path / "market.sqlite")
        (asof,) = instance.refresh_capital_policy_snapshot.call_args.args
        assert isinstance(asof, date)
        instance.close.assert_called_once_with()


def test_fixed_release_roundtrip_carries_l1_and_capital_l2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    narrow_release_policy(monkeypatch, datasets=DATASETS, carries_history=None)
    source_path = market_store(tmp_path / "source.sqlite")
    body = _corrected_workbook()
    JPXRefresh(sqlite_path=source_path, session=_session(body)).refresh_capital_policy_snapshot(
        ASOF
    )
    bundle_id = _bundle_ids(source_path)[0]
    expected_rows = _rows(source_path)
    dataset = LAKE_DATASETS["tse.capital_policy_snapshots"]
    assert dataset.layer == "l2_processed"
    assert dataset.coverage_authority == "unproven"
    mirror = tmp_path / "mirror"
    built = datetime(2026, 10, 16, tzinfo=UTC)
    with sealed_sqlite_snapshot(sqlite_path=source_path, mirror_root=mirror) as snapshot:
        manifests = [
            export_dataset_snapshot(
                dataset_name=name,
                mirror_root=mirror,
                source_snapshot=snapshot,
                producer_git_commit="a" * 40,
                build_id=f"capital-{name.replace('.', '-')}",
                created_at=built,
            ).manifest_path
            for name in DATASETS
        ]
    manifest_path, manifest = create_release(
        dataset_manifest_paths=manifests,
        mirror_root=mirror,
        release_id="capital-policy-fixed",
        created_at=built,
    )
    source = LocalMirrorSource(mirror)
    release = resolve_release(
        source, manifest.release_id, manifest_sha256=sha256_file(manifest_path)
    )
    destination = tmp_path / "hydrated.sqlite"
    shutil.copyfile(source_path, destination)
    with open_connection(destination) as conn:
        for table in ("jpx_source_responses", "jpx_source_bundles", "tse_capital_policy_snapshots"):
            conn.execute(f"DELETE FROM {table}")
        write_lake_store_origin(conn, LakeStoreOrigin(release.release_id, release.manifest_sha256))
        conn.commit()
    cache = LakeObjectCache(root=tmp_path / "cache", source=source)
    with lake_session() as lake:
        report = hydrate_market_store(
            lake, release=release, cache=cache, store=destination, dataset_names=DATASETS
        )
    assert report.rows["tse.capital_policy_snapshots"] == len(expected_rows)
    assert _rows(destination) == expected_rows
    with sqlite3.connect(destination) as conn:
        retained = read_complete_bundle(conn, bundle_id)
    assert retained.responses[0].response.body == body
    with open_connection(destination) as conn:
        conn.execute("DELETE FROM tse_capital_policy_snapshots")
        conn.commit()
    rebuild_jpx_snapshot(destination, bundle_id)
    assert _rows(destination) == expected_rows
