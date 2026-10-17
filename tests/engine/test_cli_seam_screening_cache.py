"""CLI-seam tests for the screening cache maintenance commands.

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
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.engine.test_screening_cli import (
    FakeEDINETProvider,
    FakeEDINETReader,
    FakeJPXProvider,
    FakeJQuantsProvider,
    _edinet_csv_zip,
    _edinet_document,
)
from tests.helpers.screening_sqlite import add_source_coverage, insert_daily_bars_from_closes

from baibai_engine.collection.edinet.archives import retain_source_archive
from baibai_engine.collection.edinet.models import EdinetAcquisitionError
from baibai_engine.market.edinet_metrics.store import read_edinet_metrics
from baibai_engine.market.edinet_reader import EDINETReader
from baibai_engine.screening.cli import app
from baibai_engine.screening.cli.cache import (
    FIN_SUMMARY_REVISION_OVERLAP_DAYS,
    refresh_edinet_documents_command,
)
from baibai_engine.screening.cli.delisting_cli import build_tender_offer_exits_command
from baibai_engine.screening.cli.providers import ProviderBundle
from baibai_engine.screening.cli.valuation_catalyst_cli import backfill_edinet_identity_command
from baibai_engine.screening.metrics.snapshot import (
    FIN_INPUT_WINDOW_DAYS,
    NORMALIZED_EPS_HISTORY_WINDOW_DAYS,
)
from baibai_engine.storage.observations.ingest.edinet import (
    store_edinet_documents,
    store_edinet_metrics,
)
from baibai_engine.storage.observations.schema import open_connection

# The store path every command below derives from the working directory.
_STORE = Path("stores/market/market.sqlite")


@dataclass
class _SeamEDINETProvider(FakeEDINETProvider):
    """The shared EDINET fake plus the document-state refresh the CLI seam calls."""

    refresh_calls: list[date] = field(default_factory=list)
    close_count: int = 0

    def close(self) -> None:
        self.close_count += 1

    def refresh_document_state(self, asof_date: date) -> dict[str, int]:
        self.refresh_calls.append(asof_date)
        return {"listed": 3, "refreshed": 3}

    def backfill_document_identity(self, start: date, end: date) -> dict[str, int]:
        return {"days": (end - start).days + 1, "documents": 0}


@dataclass
class _StoredSeamEDINETProvider(_SeamEDINETProvider):
    def list_documents(self, on_date: date) -> list[dict[str, object]]:
        documents = super().list_documents(on_date)
        store_edinet_documents(_STORE, on_date, documents)
        return documents

    def download_csv_zip(self, doc_id: str) -> bytes:
        content = super().download_csv_zip(doc_id)
        with contextlib.closing(open_connection(_STORE)) as connection:
            return retain_source_archive(
                connection, doc_id, 5, content, stored_on=date(2026, 4, 24)
            )


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
        patch.object(app, "JQuantsReader", lambda *_args, **_kwargs: jquants),
        patch.object(app, "JQuantsRefresh", lambda *_args, **_kwargs: jquants),
        patch.object(app, "EDINETProvider", lambda *_args, **_kwargs: edinet),
        patch.object(app, "JPXProvider", lambda *_args, **_kwargs: jpx),
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
            app, "EDINETProvider", side_effect=AssertionError("EDINET provider constructed")
        ) as edinet,
        patch.object(
            app, "JQuantsReader", side_effect=AssertionError("J-Quants reader constructed")
        ) as jquants,
        patch.object(
            app, "JQuantsRefresh", side_effect=AssertionError("J-Quants refresh constructed")
        ) as refresh,
        patch.object(
            app, "JPXProvider", side_effect=AssertionError("JPX provider constructed")
        ) as jpx,
        patch.object(
            app, "ProviderBundle", side_effect=AssertionError("provider bundle constructed")
        ) as bundle,
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        exit_code = app.main(argv)
    for constructor in (session, edinet, jquants, refresh, jpx, bundle):
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
    assert edinet.bootstrap_calls == [(asof - timedelta(days=FIN_INPUT_WINDOW_DAYS), asof)]
    assert jpx.bootstrap_calls == [asof]
    assert "bootstrap-cache done" in result.stdout


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
        patch.object(app, "JQuantsReader", return_value=jquants),
        patch.object(app, "JQuantsRefresh", return_value=jquants),
        patch.object(app, "EDINETProvider", return_value=_SeamEDINETProvider()),
        patch.object(app, "JPXProvider", return_value=FakeJPXProvider()) as jpx_factory,
    ):
        assert app.main(["bootstrap-cache", "--asof", "2026-05-08"]) == 0

    assert jpx_factory.call_args.kwargs["regulation_urls"] == {
        "特別注意銘柄": "https://www.jpx.co.jp/listing/measures/alert/index.html",
        "整理銘柄": "https://www.jpx.co.jp/listing/market-alerts/supervision/",
        "取引停止": "https://www.jpx.co.jp/markets/equities/suspended/",
        "上場廃止警告": "https://www.jpx.co.jp/listing/stocks/delisted/",
    }


def test_main_refresh_edinet_documents_refreshes_document_state_for_the_argv_asof() -> None:
    edinet = _SeamEDINETProvider()

    result = _run_cli(
        ["refresh-edinet-documents", "--asof", "2026-05-08"],
        jquants=FakeJQuantsProvider(),
        edinet=edinet,
        jpx=FakeJPXProvider(),
    )

    assert result.exit_code == 0, result.stderr
    assert edinet.refresh_calls == [date(2026, 5, 8)]
    assert edinet.close_count == 1
    assert "refresh-edinet-documents: listed=3, refreshed=3" in result.stdout


@pytest.mark.parametrize(
    ("argv", "handler", "provider_arg"),
    [
        (["bootstrap-cache", "--asof", "2026-05-08"], "bootstrap_cache_command", "providers"),
        (
            ["refresh-edinet-documents", "--asof", "2026-05-08"],
            "refresh_edinet_documents_command",
            "providers",
        ),
        (
            ["backfill-edinet-identity", "--start", "2026-05-07", "--end", "2026-05-08"],
            "backfill_edinet_identity_command",
            "providers",
        ),
        (
            ["refresh-edinet-metrics", "--asof", "2026-05-08", "--lookback-days", "0"],
            "refresh_edinet_metrics_command",
            "acquisition",
        ),
        (
            ["build-tender-offer-exits", "--asof", "2026-05-08"],
            "build_tender_offer_exits_command",
            "providers",
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


def test_main_run_does_not_construct_an_edinet_acquisition_provider() -> None:
    with (
        patch.dict(
            os.environ,
            {"JQUANTS_API_KEY": "test-jquants-key", "EDINET_API_KEY": "test-edinet-key"},
        ),
        patch.object(
            app, "EDINETProvider", side_effect=AssertionError("acquisition constructed")
        ) as factory,
        patch.object(app, "verify_screening_sqlite_coverage", return_value=[]),
        patch.object(app, "load_screening_rules") as rules,
        patch.object(app, "run_command", return_value=0) as run,
    ):
        rules.return_value.universe.required_jpx_flags = ()
        assert app.main(["run", "--asof", "2026-05-08"]) == 0
    factory.assert_not_called()
    providers = run.call_args.args[2]
    assert isinstance(providers.edinet, EDINETReader)
    assert providers.edinet_refresh is None


def test_maintenance_commands_do_not_fall_back_to_the_reader(tmp_path: Path) -> None:
    providers = ProviderBundle(
        jquants=FakeJQuantsProvider(),
        edinet=FakeEDINETReader(),
        jpx=FakeJPXProvider(),
    )
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        assert (
            refresh_edinet_documents_command(asof_date=date(2026, 5, 8), providers=providers) == 1
        )
        assert (
            backfill_edinet_identity_command(
                start=date(2026, 5, 7), end=date(2026, 5, 8), providers=providers
            )
            == 1
        )
        assert (
            build_tender_offer_exits_command(
                sqlite_path=tmp_path / "market.sqlite",
                asof_date=date(2026, 5, 8),
                providers=providers,
            )
            == 1
        )
    assert stderr.getvalue().count("EDINET provider is not configured") == 3
    assert not (tmp_path / "market.sqlite").exists()


def test_main_extract_edinet_facts_closes_provider_on_exception() -> None:
    edinet = _SeamEDINETProvider()
    with (
        patch.object(app, "extract_edinet_facts_command", side_effect=RuntimeError("synthetic")),
        pytest.raises(RuntimeError, match="synthetic"),
    ):
        _run_cli(
            ["extract-edinet-facts", "--asof", "2026-05-08"],
            edinet=edinet,
        )
    assert edinet.close_count == 1


def test_main_backfill_history_covers_every_range_source_over_the_argv_window() -> None:
    # The weekly margin balance dates are derived from stored trading days, so the
    # window needs a store that already prices it; without one the command reports
    # the empty week list as a failure rather than as a backfill that fetched nothing.
    for friday in (date(2026, 1, 9), date(2026, 1, 16), date(2026, 1, 23), date(2026, 1, 30)):
        insert_daily_bars_from_closes(_STORE, "7203", [1.0] * 5, end_date=friday)
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
    # The window's own final week is still open at `end`, so the three completed
    # weeks are the balance dates the argv window resolves to.
    assert [
        week_end for name, week_end, _ in jquants.calls if name == "get_mkt_margin_interest_week"
    ] == [date(2026, 1, 9), date(2026, 1, 16), date(2026, 1, 23)]
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


def test_main_refresh_edinet_metrics_acquires_only_needed_archive() -> None:
    edinet = _StoredSeamEDINETProvider(
        documents=[{**_edinet_document(ticker="96820", doc_id="S100TEST"), "seqNumber": 1}],
        zip_by_doc_id={"S100TEST": _edinet_csv_zip()},
    )
    result = _run_cli(
        ["refresh-edinet-metrics", "--asof", "2026-04-24", "--lookback-days", "0"],
        edinet=edinet,
    )
    assert result.exit_code == 0, result.stderr
    assert edinet.download_calls == ["S100TEST"]
    assert edinet.close_count == 1
    payload = read_edinet_metrics(_STORE, date(2026, 4, 24))
    assert payload is not None
    assert payload["9682"].net_cash == 600.0


def test_main_refresh_edinet_metrics_reports_source_acquisition_failure() -> None:
    class FailingEDINET(_SeamEDINETProvider):
        def list_documents(self, on_date: date) -> list[dict[str, object]]:
            del on_date
            raise EdinetAcquisitionError("EDINET request failed with status 403")

    edinet = FailingEDINET()
    result = _run_cli(
        ["refresh-edinet-metrics", "--asof", "2026-04-24", "--lookback-days", "0"],
        jquants=FakeJQuantsProvider(),
        edinet=edinet,
        jpx=FakeJPXProvider(),
    )
    assert result.exit_code == 1
    assert "EdinetAcquisitionError" in result.stderr
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
