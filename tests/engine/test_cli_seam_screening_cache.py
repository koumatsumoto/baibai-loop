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
    FakeJPXProvider,
    FakeJQuantsProvider,
    _edinet_csv_zip,
    _edinet_document,
)
from tests.helpers.screening_sqlite import add_source_coverage, insert_daily_bars_from_closes

from baibai_engine.market.sqlite import open_connection
from baibai_engine.screening import cli as screening_cli
from baibai_engine.screening.cli import app
from baibai_engine.screening.cli.cache import FIN_SUMMARY_REVISION_OVERLAP_DAYS
from baibai_engine.screening.metrics import BARS_INPUT_WINDOW_DAYS, FIN_INPUT_WINDOW_DAYS
from baibai_engine.screening.sqlite_reader import read_edinet_metrics

# The store path every command below derives from the working directory.
_STORE = Path("stores/market/market.sqlite")


@dataclass
class _SeamEDINETProvider(FakeEDINETProvider):
    """The shared EDINET fake plus the document-state refresh the CLI seam calls."""

    refresh_calls: list[date] = field(default_factory=list)

    def refresh_document_state(self, asof_date: date) -> dict[str, int]:
        self.refresh_calls.append(asof_date)
        return {"listed": 3, "refreshed": 3}


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
        patch.object(app, "JQuantsProvider", lambda *_args, **_kwargs: jquants),
        patch.object(app, "EDINETProvider", lambda *_args, **_kwargs: edinet),
        patch.object(app, "JPXProvider", lambda *_args, **_kwargs: jpx),
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        exit_code = screening_cli.main(argv)
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
        asof - timedelta(days=BARS_INPUT_WINDOW_DAYS),
        asof,
    ) in jquants.calls
    assert (
        "refresh_fin_summary_range",
        asof - timedelta(days=FIN_INPUT_WINDOW_DAYS),
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
    assert "refresh-edinet-documents: listed=3, refreshed=3" in result.stdout


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
    edinet = _SeamEDINETProvider(
        documents=[_edinet_document(ticker="96820", doc_id="S100TEST")],
        zip_by_doc_id={"S100TEST": _edinet_csv_zip()},
    )

    result = _run_cli(
        ["extract-edinet-metrics", "--asof", "2026-04-24", "--lookback-days", "0"],
        jquants=FakeJQuantsProvider(),
        edinet=edinet,
        jpx=FakeJPXProvider(),
    )

    assert result.exit_code == 0, result.stderr
    # `--lookback-days 0` reaches the command as an int, so the listing is the
    # as-of day alone rather than the 540-day default window.
    assert "listing EDINET documents 2026-04-24..2026-04-24 (1 day(s))" in result.stdout
    payload = read_edinet_metrics(_STORE, date(2026, 4, 24))
    assert payload is not None
    assert payload["9682"].net_cash == 600.0


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
