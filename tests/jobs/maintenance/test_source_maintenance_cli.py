"""CLI-seam tests for the current source-maintenance commands.

Every test enters through ``main()`` with the argv a human types. The string that
argparse hands over for ``--asof`` / ``--start`` / ``--end`` / ``--lookback-days``
becomes a ``date`` or an ``int`` only on that path, and only there does the
converted value get wired into the command call; a test that invokes the command
function with values it constructed itself asserts nothing about either step.

One normal path per subcommand, asserting exit code 0 together with the effect the
command exists to produce.

The tests stay offline by replacing the provider constructors only. The parser, the
dispatch, and every store write the commands perform themselves are the real ones,
so the seam under test is never the thing being faked.
"""

from __future__ import annotations

import contextlib
import io
import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from tests.helpers.edinet_documents import store_edinet_documents
from tests.helpers.edinet_metrics import _edinet_csv_zip, _edinet_document
from tests.helpers.edinet_native import native_edinet_store
from tests.helpers.http_doubles import FakeSession, response
from tests.helpers.market_maintenance import (
    FakeEDINETProvider,
    FakeJPXProvider,
    FakeJQuantsProvider,
)
from tests.helpers.screening_sqlite import add_source_coverage

from baibai_engine.data_collection.providers.edinet.archives import retain_source_archive
from baibai_engine.data_collection.providers.edinet.list_native import HEAD_KEY
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.store import (
    read_edinet_metrics,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.write import (
    store_edinet_metrics,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.maintenance.market import cli as app
from baibai_jobs.maintenance.market.cache import (
    FIN_SUMMARY_REVISION_OVERLAP_DAYS,
    NORMALIZED_EPS_HISTORY_WINDOW_DAYS,
)

# The store path every command below derives from the working directory.
_STORE = Path("stores/market/market.sqlite")


@dataclass
class _SeamEDINETProvider(FakeEDINETProvider):
    """The shared EDINET acquisition fake with explicit client lifetime."""

    close_count: int = 0

    def close(self) -> None:
        self.close_count += 1


@dataclass(frozen=True, slots=True)
class _CliResult:
    exit_code: int
    stdout: str
    stderr: str


def _run_cli(
    argv: list[str],
    *,
    jquants: FakeJQuantsProvider | None = None,
    edinet: _SeamEDINETProvider | None = None,
    jpx: FakeJPXProvider | None = None,
) -> _CliResult:
    """Run ``argv`` through ``main()`` with the provider constructors replaced.

    The API keys are forced to test values so the run cannot pick up a real
    credential, and the three provider classes are the only substitution: argparse,
    the argument conversion, and the dispatch all execute unchanged.
    """
    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        patch.dict(
            os.environ,
            {"JQUANTS_API_KEY": "test-jquants-key", "EDINET_API_KEY": "test-edinet-key"},
        ),
        patch.object(app, "JQuantsRefresh", lambda *_args, **_kwargs: jquants),
        patch.object(app, "EDINETRefresh", lambda *_args, **_kwargs: edinet),
        patch.object(app, "JPXRefresh", lambda *_args, **_kwargs: jpx),
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        exit_code = app.main(argv)
    return _CliResult(exit_code=exit_code, stdout=stdout.getvalue(), stderr=stderr.getvalue())


def _run_offline_metrics_cli(argv: list[str]) -> _CliResult:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        patch.dict(os.environ, {"EDINET_API_KEY": "", "JQUANTS_API_KEY": ""}),
        patch.object(app, "load_project_env", return_value=None),
        patch(
            "requests.Session", side_effect=AssertionError("HTTP session constructed")
        ) as session,
        patch.object(
            app, "EDINETRefresh", side_effect=AssertionError("EDINET provider constructed")
        ) as edinet,
        patch.object(
            app, "JQuantsRefresh", side_effect=AssertionError("J-Quants refresh constructed")
        ) as refresh,
        patch.object(
            app, "JPXRefresh", side_effect=AssertionError("JPX provider constructed")
        ) as jpx,
        patch.object(
            app, "ProviderBundle", side_effect=AssertionError("provider bundle constructed")
        ) as bundle,
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        exit_code = app.main(argv)
    for constructor in (session, edinet, refresh, jpx, bundle):
        constructor.assert_not_called()
    return _CliResult(exit_code=exit_code, stdout=stdout.getvalue(), stderr=stderr.getvalue())


@pytest.fixture(autouse=True)
def _work_in_tmp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the store path, and the project ``.env`` lookup, inside the tmp tree."""
    monkeypatch.chdir(tmp_path)


def test_main_bootstrap_cache_fetches_each_source_window_around_the_argv_asof() -> None:
    jquants = FakeJQuantsProvider()
    edinet = _SeamEDINETProvider()
    jpx = FakeJPXProvider()

    result = _run_cli(
        ["bootstrap-cache", "--asof", "2026-05-08"], jquants=jquants, edinet=edinet, jpx=jpx
    )

    assert result.exit_code == 0, result.stderr
    asof = date(2026, 5, 8)
    assert ("get_eq_master", asof, asof) in jquants.calls
    # Bootstrap covers the windows and reports counts; it never asks for the rows.
    # The bar window alone is millions of them and the command prints one number.
    assert (
        "ensure_eq_bars_daily_range",
        asof - timedelta(days=NORMALIZED_EPS_HISTORY_WINDOW_DAYS),
        asof,
    ) in jquants.calls
    assert (
        "refresh_fin_summary_range",
        asof - timedelta(days=NORMALIZED_EPS_HISTORY_WINDOW_DAYS),
        asof,
    ) in jquants.calls
    assert edinet.bootstrap_calls == []
    assert edinet.close_count == 0
    assert jpx.bootstrap_calls == [asof]
    assert jpx.close_count == 1
    assert "bootstrap-cache done" in result.stdout


def test_main_bootstrap_routes_and_closes_maintenance() -> None:
    jpx = FakeJPXProvider()
    with patch.object(app, "bootstrap_cache_command", return_value=0) as command:
        result = _run_cli(
            ["bootstrap-cache", "--asof", "2026-05-08"],
            jquants=FakeJQuantsProvider(),
            edinet=_SeamEDINETProvider(),
            jpx=jpx,
        )

    assert result.exit_code == 0, result.stderr
    providers = command.call_args.kwargs["providers"]
    assert providers.jpx_refresh is jpx
    assert jpx.close_count == 1


def test_main_bootstrap_cache_asks_for_the_revision_overlap_once() -> None:
    """The 730-day and 2,200-day windows share one source and one trailing week.

    Requesting the overlap on each would pay J-Quants twice for the same days,
    which is most of what deriving the window from coverage was meant to save.
    """
    jquants = FakeJQuantsProvider()

    result = _run_cli(
        ["bootstrap-cache", "--asof", "2026-05-08"],
        jquants=jquants,
        edinet=_SeamEDINETProvider(),
        jpx=FakeJPXProvider(),
    )

    assert result.exit_code == 0, result.stderr
    assert jquants.revision_overlap_days == [FIN_SUMMARY_REVISION_OVERLAP_DAYS]


def test_main_bootstrap_cache_uses_fixed_jpx_urls_despite_stale_env() -> None:
    jquants = FakeJQuantsProvider()
    with (
        patch.dict(
            os.environ,
            {
                "JQUANTS_API_KEY": "test-jquants-key",
                "EDINET_API_KEY": "test-edinet-key",
                "JPX_SPECIAL_CAUTION_URL": "https://example.com/alert",
                "JPX_REORGANIZATION_URL": "https://example.com/supervision",
                "JPX_TRADING_HALT_URL": "https://example.com/halt",
                "JPX_DELISTING_WARNING_URL": "https://example.com/delisted",
            },
        ),
        patch.object(app, "JQuantsRefresh", return_value=jquants),
        patch.object(app, "EDINETRefresh", return_value=_SeamEDINETProvider()),
        patch.object(app, "JPXRefresh", return_value=FakeJPXProvider()) as jpx_factory,
    ):
        assert app.main(["bootstrap-cache", "--asof", "2026-05-08"]) == 0

    assert jpx_factory.call_args.kwargs["sqlite_path"].name == "market.sqlite"


@pytest.mark.parametrize(
    ("argv", "handler", "provider_arg"),
    [
        (
            ["refresh-edinet-metrics", "--asof", "2026-05-08", "--lookback-days", "0"],
            "refresh_edinet_metrics_command",
            "acquisition",
        ),
        (
            ["refresh-tender-offer-exits", "--asof", "2026-05-08"],
            "refresh_tender_offer_exits_command",
            "acquisition",
        ),
    ],
)
def test_main_explicit_edinet_commands_pass_and_close_maintenance_provider(
    argv, handler, provider_arg
) -> None:
    edinet = _SeamEDINETProvider()
    with patch.object(app, handler, return_value=0) as command:
        result = _run_cli(argv, edinet=edinet)
    assert result.exit_code == 0, result.stderr
    routed = command.call_args.kwargs[provider_arg]
    assert (routed if provider_arg == "acquisition" else routed.edinet_refresh) is edinet
    assert edinet.close_count == 1


def test_main_refresh_edinet_facts_closes_provider_on_exception() -> None:
    edinet = _SeamEDINETProvider()
    with (
        patch.object(app, "refresh_edinet_facts_command", side_effect=RuntimeError("synthetic")),
        pytest.raises(RuntimeError, match="synthetic"),
    ):
        _run_cli(
            ["refresh-edinet-facts", "--asof", "2026-05-08"],
            edinet=edinet,
        )
    assert edinet.close_count == 1


def test_main_refresh_tender_offer_exits_closes_provider_on_exception() -> None:
    edinet = _SeamEDINETProvider()
    with (
        patch.object(
            app, "refresh_tender_offer_exits_command", side_effect=RuntimeError("synthetic")
        ),
        pytest.raises(RuntimeError, match="synthetic"),
    ):
        _run_cli(["refresh-tender-offer-exits", "--asof", "2026-05-08"], edinet=edinet)
    assert edinet.close_count == 1


def test_main_refresh_tender_offer_exits_uses_the_requested_store_and_acquisition(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "other-market.sqlite"
    edinet = _SeamEDINETProvider()
    with (
        patch.dict(os.environ, {"EDINET_API_KEY": "test-edinet-key"}),
        patch.object(app, "load_project_env", return_value=None),
        patch.object(app, "EDINETRefresh", return_value=edinet) as factory,
        patch.object(app, "refresh_tender_offer_exits_command", return_value=0) as command,
    ):
        assert (
            app.main(
                [
                    "refresh-tender-offer-exits",
                    "--asof",
                    "2026-05-08",
                    "--sqlite-path",
                    str(sqlite_path),
                ]
            )
            == 0
        )
    assert factory.call_args.kwargs["sqlite_path"] == sqlite_path
    assert command.call_args.kwargs["sqlite_path"] == sqlite_path
    assert command.call_args.kwargs["acquisition"] is edinet
    assert "reader" not in command.call_args.kwargs
    assert edinet.close_count == 1


def test_main_backfill_history_covers_every_range_source_over_the_argv_window() -> None:
    jquants = FakeJQuantsProvider()

    result = _run_cli(
        ["backfill-history", "--start", "2026-01-05", "--end", "2026-01-30"],
        jquants=jquants,
        edinet=_SeamEDINETProvider(),
        jpx=FakeJPXProvider(),
    )

    assert result.exit_code == 0, result.stderr
    window = (date(2026, 1, 5), date(2026, 1, 30))
    for source in ("get_eq_bars_daily_range", "get_fin_summary_range", "get_mkt_calendar"):
        assert [call[1:] for call in jquants.calls if call[0] == source] == [window]
    assert not any("margin" in name or "short_sale" in name for name, _, _ in jquants.calls)
    assert "backfill-history done: 2026-01-05..2026-01-30" in result.stdout


def test_main_extract_edinet_metrics_stores_parsed_metrics_for_the_argv_asof() -> None:
    document = {**_edinet_document(ticker="96820", doc_id="S100TEST"), "seqNumber": 1}
    store_edinet_documents(_STORE, date(2026, 4, 24), [document])
    with contextlib.closing(open_connection(_STORE)) as connection:
        retain_source_archive(
            connection, "S100TEST", 5, _edinet_csv_zip(), stored_on=date(2026, 4, 24)
        )
    result = _run_offline_metrics_cli(
        ["extract-edinet-metrics", "--asof", "2026-04-24", "--lookback-days", "0"]
    )

    assert result.exit_code == 0, result.stderr
    # `--lookback-days 0` reaches the command as an int, so the listing is the
    # as-of day alone rather than the 540-day default window.
    assert "listing EDINET documents 2026-04-24..2026-04-24 (1 day(s))" in result.stdout
    payload = read_edinet_metrics(_STORE, date(2026, 4, 24))
    assert payload is not None
    assert payload["9682"].net_cash == 600.0


def test_main_extract_edinet_metrics_missing_archive_preserves_old_metrics() -> None:
    asof = date(2026, 4, 24)
    document = {**_edinet_document(ticker="96820", doc_id="S100TEST"), "seqNumber": 1}
    store_edinet_documents(_STORE, asof, [document])
    store_edinet_metrics(
        _STORE,
        asof,
        [{"ticker": "9682", "sales_ttm": 77.0, "extractor_revision": "previous"}],
    )

    result = _run_offline_metrics_cli(
        ["extract-edinet-metrics", "--asof", "2026-04-24", "--lookback-days", "0"]
    )

    assert result.exit_code == 1
    assert "hard_failures=1" in result.stdout
    assert "output=not-written" in result.stdout
    saved = read_edinet_metrics(_STORE, asof)
    assert saved is not None
    assert saved["9682"].sales_ttm == 77.0


@pytest.mark.parametrize("command", ["extract-edinet-metrics", "refresh-edinet-metrics"])
def test_main_rejects_negative_metric_lookback(command: str) -> None:
    if command == "extract-edinet-metrics":
        with patch.object(app, "EDINETReader", side_effect=AssertionError("reader constructed")):
            result = _run_offline_metrics_cli(
                [command, "--asof", "2026-04-24", "--lookback-days", "-1"]
            )
    else:
        edinet = _SeamEDINETProvider()
        result = _run_cli([command, "--asof", "2026-04-24", "--lookback-days", "-1"], edinet=edinet)
        assert edinet.download_calls == []
    assert result.exit_code == 1
    assert "--lookback-days must be zero or greater" in result.stderr


def test_main_refresh_edinet_metrics_acquires_only_needed_archive(tmp_path, monkeypatch) -> None:
    native_edinet_store(
        tmp_path,
        monkeypatch,
        {date(2026, 4, 24): [_edinet_document(ticker="96820", doc_id="S100TEST")]},
    )
    edinet = app.EDINETRefresh(
        "synthetic", sqlite_path=_STORE, session=FakeSession(response(_edinet_csv_zip()))
    )
    download = Mock(wraps=edinet.download_csv_zip)
    close = Mock(wraps=edinet.close)
    monkeypatch.setattr(edinet, "download_csv_zip", download)
    monkeypatch.setattr(edinet, "close", close)
    result = _run_cli(
        ["refresh-edinet-metrics", "--asof", "2026-04-24", "--lookback-days", "0"],
        edinet=edinet,
    )
    assert result.exit_code == 0, result.stderr
    download.assert_called_once_with("S100TEST")
    close.assert_called_once_with()
    payload = read_edinet_metrics(_STORE, date(2026, 4, 24))
    assert payload is not None
    assert payload["9682"].net_cash == 600.0


def test_main_refresh_edinet_metrics_reports_source_acquisition_failure(
    tmp_path, monkeypatch
) -> None:
    store, _, _ = native_edinet_store(tmp_path, monkeypatch, {date(2026, 4, 24): []})
    del store.values[HEAD_KEY]
    edinet = _SeamEDINETProvider()
    result = _run_cli(
        ["refresh-edinet-metrics", "--asof", "2026-04-24", "--lookback-days", "0"],
        jquants=FakeJQuantsProvider(),
        edinet=edinet,
        jpx=FakeJPXProvider(),
    )
    assert result.exit_code == 1
    assert "LakePublishError" in result.stderr
    assert edinet.close_count == 1


def test_main_invalidate_coverage_deletes_only_the_rows_overlapping_the_argv_window() -> None:
    conn = open_connection(_STORE)
    try:
        add_source_coverage(
            conn,
            source="jquants_daily_bars",
            coverage_key="2026-01",
            min_date="2026-01-01",
            max_date="2026-01-31",
        )
        add_source_coverage(
            conn,
            source="jquants_daily_bars",
            coverage_key="2025-01",
            min_date="2025-01-01",
            max_date="2025-01-31",
        )
        conn.commit()
    finally:
        conn.close()

    result = _run_cli(
        [
            "invalidate-coverage",
            "--source",
            "jquants_daily_bars",
            "--start",
            "2026-01-10",
            "--end",
            "2026-01-20",
            "--sqlite-path",
            str(_STORE),
        ]
    )

    assert result.exit_code == 0, result.stderr
    conn = open_connection(_STORE)
    try:
        remaining = conn.execute(
            "SELECT coverage_key FROM source_coverage WHERE source = ?",
            ("jquants_daily_bars",),
        ).fetchall()
    finally:
        conn.close()
    assert [row[0] for row in remaining] == ["2025-01"]
    assert "invalidate-coverage: removing 1 source_coverage row(s)" in result.stdout
    assert "stored observations unchanged" in result.stdout
