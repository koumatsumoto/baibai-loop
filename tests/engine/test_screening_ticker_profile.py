from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import pytest
from tests.helpers.db_seed import seed_ledger
from tests.helpers.screening_run import screening_run_payload, security_analysis
from tests.helpers.screening_sqlite import insert_daily_bars_from_closes

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.ledger import PortfolioLedgerDocument
from baibai_engine.screening.cli import build_parser, ticker_profile_command
from baibai_engine.screening.discovery import build_review_set
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.run_store import ScreeningRunStore
from baibai_engine.screening.sqlite_cache import open_connection
from baibai_engine.screening.ticker_profile import _load_bars, build_ticker_profile

_ASOF = date(2026, 5, 29)


def _insert_bars(sqlite_path: Path, ticker: str, closes: list[float], *, end: date) -> None:
    insert_daily_bars_from_closes(sqlite_path, ticker, closes, end_date=end, turnover_value=2.0e8)


def _insert_reference_rows(sqlite_path: Path) -> None:
    conn = open_connection(sqlite_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO jquants_master_snapshots"
            "(snapshot_date, ticker, name, market, sector_33, is_common_stock)"
            " VALUES ('2026-05-28', 'AAAA', 'テスト製作所', 'プライム', '機械', 1)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO jquants_master_snapshots"
            "(snapshot_date, ticker, name, market, sector_33, is_common_stock)"
            " VALUES ('2026-05-28', 'BBBB', '同業ペア', 'プライム', '機械', 1)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO jpx_earnings_calendar"
            "(announcement_date, ticker) VALUES ('2026-06-10', 'AAAA')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO source_coverage("
            "source, coverage_key, coverage_start, coverage_end, fetched_at_utc, "
            "record_count, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "jpx_earnings_calendar",
                "get_earnings_calendar_snapshot:current",
                "2026-06-10",
                "2026-06-10",
                "2026-05-29T00:00:00+09:00",
                1,
                "ok",
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO jpx_regulation_flags"
            "(asof_date, source_name, ticker, flag)"
            " VALUES ('2026-05-29', '特別注意銘柄', 'AAAA', '特別注意銘柄')"
        )
        conn.commit()
    finally:
        conn.close()


def _write_security_analyses(runs_db_path: Path) -> None:
    ScreeningRunStore(runs_db_path).publish_run(
        screening_run_payload(
            as_of="2026-05-29",
            run_at="2026-05-29T09:00:00+09:00",
            universe_size=1,
            security_analyses=[
                security_analysis(
                    "AAAA",
                    name="テスト製作所",
                    metrics={"ocf_yield": 0.11},
                )
            ],
            filters={},
            generated_by="test",
            data_sources=[],
            provider_status_lines=[],
            universe_exclusion_lines=[],
            ttm_quality_counts={},
            evidence_hits_summary={},
            fallback_lines=[],
        ),
        run_revision_id="run-20260529-test",
    )


class BuildTickerProfileTests(unittest.TestCase):
    def _build(self, root: Path, ticker: str) -> dict[str, object]:
        return build_ticker_profile(
            sqlite_path=root / "market.sqlite",
            ticker=ticker,
            asof_date=_ASOF,
            runs_db_path=root / "runs.sqlite",
            app_db_path=root / "app.sqlite",
        )

    def test_legacy_jquants_earnings_rows_are_not_reported_as_jpx_fact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sqlite_path = root / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.execute(
                "INSERT INTO jpx_earnings_calendar(announcement_date, ticker) "
                "VALUES ('2026-06-10', 'AAAA')"
            )
            conn.execute(
                "INSERT INTO source_coverage(source, coverage_key, coverage_start, "
                "coverage_end, fetched_at_utc, record_count, status) "
                "VALUES ('jpx_earnings_calendar', 'legacy', '2026-05-29', "
                "'2026-08-27', '2026-05-29T00:00:00+09:00', 1, 'ok')"
            )
            conn.commit()
            conn.close()

            profile = self._build(root, "AAAA")

            events = profile["events"]
            assert isinstance(events, dict)
            self.assertIsNone(events["next_earnings_date"])

    def test_profile_covers_price_relative_events_and_screening(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sqlite_path = root / "market.sqlite"
            # 30 bars rising 1%/day; benchmark flat; one sector peer falling.
            _insert_bars(sqlite_path, "AAAA", [100 * 1.01**i for i in range(30)], end=_ASOF)
            _insert_bars(sqlite_path, "1321", [200.0] * 30, end=_ASOF)
            _insert_bars(sqlite_path, "BBBB", [300 * 0.99**i for i in range(30)], end=_ASOF)
            _insert_reference_rows(sqlite_path)
            _write_security_analyses(root / "runs.sqlite")

            profile = self._build(root, "AAAA")

            master = profile["master"]
            assert isinstance(master, dict)
            self.assertEqual(master["sector_33"], "機械")
            price = profile["price"]
            assert isinstance(price, dict)
            self.assertEqual(price["resolved_date"], _ASOF.isoformat())
            assert isinstance(price["return_5d"], float)
            self.assertAlmostEqual(price["return_5d"], 1.01**5 - 1, places=9)
            self.assertEqual(price["bar_count"], 30)
            self.assertAlmostEqual(price["avg_turnover_20d_oku"], 2.0, places=9)
            relative = profile["relative"]
            assert isinstance(relative, dict)
            assert isinstance(relative["relative_5d"], float)
            self.assertAlmostEqual(relative["relative_5d"], 1.01**5 - 1, places=9)
            sector = relative["sector"]
            assert isinstance(sector, dict)
            self.assertEqual(sector["peer_count"], 1)
            assert isinstance(sector["peer_median_return_20d"], float)
            self.assertLess(sector["peer_median_return_20d"], 0)
            events = profile["events"]
            assert isinstance(events, dict)
            self.assertEqual(events["next_earnings_date"], "2026-06-10")
            jpx = events["jpx_regulation"]
            assert isinstance(jpx, dict)
            self.assertEqual(jpx["flags"], ["特別注意銘柄"])
            screening = profile["screening"]
            assert isinstance(screening, dict)
            self.assertTrue(screening["has_security_analysis"])
            security_analysis = screening["security_analysis"]
            assert isinstance(security_analysis, dict)
            self.assertEqual(security_analysis["metrics"], {"ocf_yield": 0.11})

    def test_price_series_keeps_an_action_event_without_a_close(self) -> None:
        """売買停止日のfactorを落とさず、前後価格を同じ株式基準へ揃える。"""

        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            conn = open_connection(sqlite_path)
            try:
                conn.executemany(
                    "INSERT INTO jquants_daily_bars("
                    "ticker, traded_at, close, turnover_value, adjustment_factor"
                    ") VALUES (?, ?, ?, ?, ?)",
                    [
                        ("6731", "2023-12-26", 1.0, 2.0e8, 1.0),
                        ("6731", "2023-12-27", None, None, 100.0),
                        ("6731", "2023-12-29", 100.0, 2.0e8, 1.0),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            with_event = _load_bars(
                sqlite_path,
                tickers=("6731",),
                start=date(2023, 12, 26),
                end=date(2023, 12, 29),
            )["6731"]
            conn = open_connection(sqlite_path)
            try:
                conn.execute(
                    "UPDATE jquants_daily_bars SET adjustment_factor = 1 "
                    "WHERE ticker = ? AND traded_at = ?",
                    ("6731", "2023-12-27"),
                )
                conn.commit()
            finally:
                conn.close()
            without_event = _load_bars(
                sqlite_path,
                tickers=("6731",),
                start=date(2023, 12, 26),
                end=date(2023, 12, 29),
            )["6731"]

            self.assertEqual([bar.price for bar in with_event], [100.0, 100.0])
            self.assertEqual([bar.price for bar in without_event], [1.0, 100.0])

    def test_profile_degrades_explicitly_for_unknown_ticker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _insert_bars(root / "market.sqlite", "1321", [200.0] * 30, end=_ASOF)

            profile = self._build(root, "ZZZZ")

            self.assertIsNone(profile["master"])
            self.assertIsNone(profile["price"])
            self.assertIsNone(profile["relative"])
            screening = profile["screening"]
            assert isinstance(screening, dict)
            self.assertFalse(screening["has_security_analysis"])

    def test_profile_marks_ticker_missing_from_security_analyses(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sqlite_path = root / "market.sqlite"
            _insert_bars(sqlite_path, "BBBB", [300.0] * 30, end=_ASOF)
            _insert_reference_rows(sqlite_path)
            _write_security_analyses(root / "runs.sqlite")

            profile = self._build(root, "BBBB")

            screening = profile["screening"]
            assert isinstance(screening, dict)
            self.assertFalse(screening["has_security_analysis"])
            self.assertIn("no Security Analysis", str(screening["note"]))
            self.assertNotIn("security_analysis", screening)

    def test_profile_retains_security_analysis_without_nomination(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_security_analyses(root / "runs.sqlite")
            profile = self._build(root, "AAAA")
            screening = profile["screening"]
            assert isinstance(screening, dict)
            analysis = screening["security_analysis"]
            assert isinstance(analysis, dict)
            rules = load_screening_rules()
            review_set = build_review_set(
                [analysis],
                rules=rules.candidate_discovery,
                required_jpx_flags=rules.universe.required_jpx_flags,
            )
            self.assertEqual(review_set["entries"], [])
            self.assertTrue(screening["has_security_analysis"])
            self.assertNotIn("note", screening)

    def test_screening_uses_only_latest_stored_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runs_path = root / "runs.sqlite"
            _write_security_analyses(runs_path)
            latest = ScreeningRunStore(runs_path).publish_run(
                screening_run_payload(
                    as_of="2026-05-30",
                    run_at="2026-05-30T09:00:00+09:00",
                    universe_size=1,
                    security_analyses=[security_analysis("BBBB", name="同業ペア", metrics={})],
                    filters={},
                    generated_by="test",
                    data_sources=[],
                    provider_status_lines=[],
                    fallback_lines=[],
                )
            )

            profile = build_ticker_profile(
                sqlite_path=root / "market.sqlite",
                ticker="AAAA",
                asof_date=date(2026, 5, 30),
                runs_db_path=runs_path,
                app_db_path=root / "app.sqlite",
            )

            screening = profile["screening"]
            assert isinstance(screening, dict)
            self.assertEqual(screening["screening_run_revision_id"], latest.publication_id)
            self.assertEqual(screening["screening_run_as_of"], "2026-05-30")
            self.assertFalse(screening["has_security_analysis"])

    def test_master_and_sector_peers_use_only_latest_global_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sqlite_path = root / "market.sqlite"
            _insert_bars(sqlite_path, "AAAA", [100.0] * 30, end=_ASOF)
            _insert_bars(sqlite_path, "BBBB", [300 * 0.99**i for i in range(30)], end=_ASOF)
            _insert_bars(sqlite_path, "CCCC", [200 * 1.01**i for i in range(30)], end=_ASOF)
            _insert_reference_rows(sqlite_path)
            conn = open_connection(sqlite_path)
            try:
                conn.executemany(
                    "INSERT INTO jquants_master_snapshots"
                    "(snapshot_date, ticker, name, market, sector_33, is_common_stock)"
                    " VALUES ('2026-05-29', ?, ?, 'プライム', '機械', 1)",
                    (
                        ("AAAA", "テスト製作所"),
                        ("CCCC", "現構成ペア"),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            current_profile = self._build(root, "AAAA")
            old_only_profile = self._build(root, "BBBB")

            relative = current_profile["relative"]
            assert isinstance(relative, dict)
            sector = relative["sector"]
            assert isinstance(sector, dict)
            self.assertEqual(sector["peer_count"], 1)
            assert isinstance(sector["peer_median_return_20d"], float)
            self.assertGreater(sector["peer_median_return_20d"], 0)
            self.assertIsNone(old_only_profile["master"])


class TickerProfileCliTests(unittest.TestCase):
    def test_parser_accepts_ticker_profile_arguments(self) -> None:
        args = build_parser().parse_args(
            ["ticker-profile", "--ticker", "8303", "--asof", "2026-06-08"]
        )
        self.assertEqual(args.ticker, "8303")
        self.assertEqual(args.asof, "2026-06-08")
        self.assertTrue(args.sqlite_path.endswith("market.sqlite"))

    def test_parser_documents_latest_stored_run_default(self) -> None:
        parser = build_parser()
        action = next(
            item for item in parser._actions if isinstance(item, argparse._SubParsersAction)
        )
        help_text = " ".join(action.choices["ticker-profile"].format_help().split())
        self.assertIn("default: latest stored run", help_text)

    def test_command_rejects_invalid_ticker(self) -> None:
        exit_code = ticker_profile_command(
            ticker="not-a-ticker",
            asof="2026-06-08",
            sqlite_path=Path("/nonexistent.sqlite"),
            runs_db_path=Path("/nonexistent-runs.sqlite"),
            app_db_path=Path("/nonexistent-app.sqlite"),
            stdout=io.StringIO(),
        )
        self.assertEqual(exit_code, 1)

    def test_command_emits_yaml_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sqlite_path = root / "market.sqlite"
            _insert_bars(sqlite_path, "AAAA", [100.0] * 30, end=_ASOF)
            buffer = io.StringIO()
            exit_code = ticker_profile_command(
                ticker="AAAA",
                asof=_ASOF.isoformat(),
                sqlite_path=sqlite_path,
                runs_db_path=root / "runs.sqlite",
                app_db_path=root / "app.sqlite",
                stdout=buffer,
            )
            self.assertEqual(exit_code, 0)
            payload = safe_load(buffer.getvalue())
            self.assertEqual(payload["ticker"], "AAAA")
            price = payload["price"]
            self.assertEqual(price["bar_count"], 30)


if __name__ == "__main__":
    unittest.main()


class PortfolioBlockTests(unittest.TestCase):
    def test_portfolio_block_reports_sector_concentration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sqlite_path = root / "market.sqlite"
            _insert_bars(sqlite_path, "AAAA", [100.0] * 30, end=_ASOF)
            _insert_reference_rows(sqlite_path)
            _write_portfolio_ledger(root, ticker="BBBB", sector="機械", price_yen=500)

            profile = build_ticker_profile(
                sqlite_path=sqlite_path,
                ticker="AAAA",
                asof_date=_ASOF,
                runs_db_path=root / "runs.sqlite",
                app_db_path=root / "app.sqlite",
            )

            portfolio = profile["portfolio"]
            assert isinstance(portfolio, dict)
            self.assertEqual(portfolio["open_position_count"], 1)
            self.assertFalse(portfolio["holds_this_ticker"])
            # BBBB shares AAAA's sector (機械) so the whole notional concentrates there.
            self.assertEqual(portfolio["same_sector_position_count"], 1)
            self.assertEqual(portfolio["same_sector_entry_notional_share"], 1.0)
            positions = portfolio["open_positions"]
            assert isinstance(positions, list)
            self.assertEqual(positions[0]["entry_notional_yen"], 50000)


def _write_portfolio_ledger(root: Path, *, ticker: str, sector: str, price_yen: int) -> None:
    seed_ledger(
        root / "app.sqlite",
        PortfolioLedgerDocument.model_validate(
            {
                "schema_version": 2,
                "portfolio_scope": "repository_only",
                "as_of": "2026-05-29T15:30:00+09:00",
                "estimated_exit_tax_rate_bps": None,
                "estimated_exit_tax_basis": None,
                "events": [
                    {
                        "event_id": "opening",
                        "type": "opening_balance",
                        "occurred_at": "2026-05-14T08:00:00+09:00",
                        "amount_yen": 100000,
                    },
                    {
                        "event_id": "reservation",
                        "type": "reservation",
                        "occurred_at": "2026-05-14T08:01:00+09:00",
                        "reservation_id": "reservation-1",
                        "order_id": "order-1",
                        "ticker": ticker,
                        "sector": sector,
                        "common_factors": [],
                        "quantity": 100,
                        "price_guard_yen": price_yen,
                        "expires_at": "2026-05-14T15:30:00+09:00",
                    },
                    {
                        "event_id": "execution",
                        "type": "execution",
                        "occurred_at": "2026-05-14T09:00:00+09:00",
                        "execution_id": "execution-1",
                        "reservation_id": "reservation-1",
                        "ticker": ticker,
                        "side": "buy",
                        "quantity": 100,
                        "price_yen": price_yen,
                    },
                ],
                "market_prices": [
                    {
                        "ticker": ticker,
                        "price_yen": price_yen,
                        "observed_at": "2026-05-29T15:30:00+09:00",
                        "source_kind": "licensed_dataset",
                        "price_basis": "unadjusted_close",
                        "source_ref": "test:ledger-price",
                    }
                ],
                "overrides": [],
            }
        ),
    )


@pytest.mark.parametrize("price_state", ["missing", "stale"])
def test_portfolio_facts_survive_unavailable_ledger_valuation(tmp_path, price_state):
    """Cost concentration uses confirmed trades, including after price transcription stops."""
    from baibai_engine.position.store import LedgerStoreService
    from baibai_engine.screening.ticker_profile import _portfolio_block

    _write_portfolio_ledger(tmp_path, ticker="BBBB", sector="機械", price_yen=500)
    service = LedgerStoreService(tmp_path / "app.sqlite")
    source, head = service.load_with_head()
    prices = (
        ()
        if price_state == "missing"
        else tuple(
            price.model_copy(update={"observed_at": source.as_of - timedelta(days=30)})
            for price in source.market_prices
        )
    )
    service.apply_document(
        expected_head=head,
        expected_document=source,
        replacement=source.model_copy(update={"market_prices": prices}),
    )
    portfolio = _portfolio_block(app_db_path=tmp_path / "app.sqlite", ticker="BBBB", sector="機械")
    assert portfolio["holds_this_ticker"] is True
    assert portfolio["open_position_count"] == 1
    assert portfolio["same_sector_entry_notional_share"] == 1.0
    assert portfolio["open_positions"][0]["entry_notional_yen"] == 50000


def _insert_dated_bars(sqlite_path, ticker, prices):
    conn = open_connection(sqlite_path)
    try:
        conn.executemany(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close, adjustment_factor) "
            "VALUES (?, ?, ?, 1)",
            [(ticker, day.isoformat(), price) for day, price in prices],
        )
        conn.commit()
    finally:
        conn.close()


def _dated_profile(root, asof):
    return build_ticker_profile(
        sqlite_path=root / "market.sqlite",
        ticker="AAAA",
        asof_date=asof,
        runs_db_path=root / "runs.sqlite",
        app_db_path=root / "app.sqlite",
    )


@pytest.mark.parametrize("missing", [None, "start", "end"])
def test_relative_uses_exact_target_endpoints(tmp_path, missing):
    start, middle, end = (date(2026, 9, day) for day in (15, 16, 17))
    sqlite_path = tmp_path / "market.sqlite"
    _insert_dated_bars(sqlite_path, "AAAA", [(start, 100), (end, 121)])
    benchmark = [(start, 200), (middle, 220), (end, 242)]
    missing_date = {"start": start, "end": end}.get(missing)
    _insert_dated_bars(
        sqlite_path, "1321", [(day, price) for day, price in benchmark if day != missing_date]
    )
    profile = _dated_profile(tmp_path, end)
    assert profile["price"]["return_1d"] == pytest.approx(0.21)
    relative = profile["relative"]
    if missing is None:
        assert relative["relative_1d"] == pytest.approx(0)
    else:
        assert relative["relative_1d"] is None
    for window in (5, 20, 60):
        assert relative[f"relative_{window}d"] is None


@pytest.mark.parametrize("window", [1, 5, 20, 60])
def test_relative_loads_suspended_target_interval_and_ignores_later_bars(tmp_path, window):
    end = date(2026, 9, 17)
    start = end - timedelta(days=180)
    target = [(start, 100)] + [
        (end - timedelta(days=window - i - 1), 110 + i) for i in range(window)
    ]
    sqlite_path = tmp_path / "market.sqlite"
    _insert_dated_bars(sqlite_path, "AAAA", target)
    # Only endpoints are required, even when the benchmark has fewer bars than target.
    _insert_dated_bars(
        sqlite_path, "1321", [(start, 200), (end, 220), (end + timedelta(days=1), 500)]
    )
    profile = _dated_profile(tmp_path, end + timedelta(days=1))
    own_return = target[-1][1] / 100 - 1
    assert profile["price"][f"return_{window}d"] == pytest.approx(own_return)
    assert profile["relative"][f"relative_{window}d"] == pytest.approx(own_return - 0.1)


@pytest.mark.parametrize("target_count", [20, 21])
@pytest.mark.parametrize("usable_peers", [False, True])
def test_sector_uses_only_peers_with_exact_target_interval(tmp_path, target_count, usable_peers):
    sqlite_path = tmp_path / "market.sqlite"
    end = _ASOF
    start = end - timedelta(days=180)
    target = [(start, 100)] + [(end - timedelta(days=19 - i), 110) for i in range(20)]
    _insert_dated_bars(sqlite_path, "AAAA", target[-target_count:])
    _insert_reference_rows(sqlite_path)
    peers = {
        "BBBB": [(start, 100), (end, 120)],
        "CCCC": [(start, 100), (end, 140)],
        "DDDD": [(start + timedelta(days=1), 100), (end, 900)],
        "EEEE": [(start, 100), (end - timedelta(days=1), 900)],
    }
    if not usable_peers:
        peers.pop("BBBB")
        peers.pop("CCCC")
    for ticker, prices in peers.items():
        _insert_dated_bars(sqlite_path, ticker, prices)
    conn = open_connection(sqlite_path)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO jquants_master_snapshots"
            "(snapshot_date, ticker, name, market, sector_33, is_common_stock)"
            " VALUES ('2026-05-28', ?, 'peer', 'プライム', '機械', 1)",
            [(ticker,) for ticker in peers],
        )
        conn.commit()
    finally:
        conn.close()
    sector = _dated_profile(tmp_path, end)["relative"]["sector"]
    if target_count == 20 or not usable_peers:
        assert sector is None
    else:
        assert sector["peer_count"] == 2
        assert sector["peer_median_return_20d"] == pytest.approx(0.3)
