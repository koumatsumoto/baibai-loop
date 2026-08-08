from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests.helpers.db_seed import seed_ledger
from tests.helpers.screening_sqlite import insert_daily_bars_from_closes

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.ledger import PortfolioLedgerDocument
from baibai_engine.screening.cli import build_parser, ticker_profile_command
from baibai_engine.screening.run_store import ScreeningRunStore
from baibai_engine.screening.sqlite_cache import open_connection
from baibai_engine.screening.ticker_profile import build_ticker_profile

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
            "INSERT OR REPLACE INTO jquants_earnings_calendar"
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


def _write_candidates(runs_db_path: Path) -> None:
    ScreeningRunStore(runs_db_path).publish_run(
        {
            "run_date": "2026-05-29",
            "asof_date": "2026-05-29",
            "universe_size": 1,
            "filters": {},
            "generated_by": "test",
            "data_sources": [],
            "run_at": "2026-05-29T09:00:00+09:00",
            "run_id": "screening-20260529",
            "candidates": [
                {
                    "ticker": "AAAA",
                    "name": "テスト製作所",
                    "evidence_hits": [
                        {
                            "name": "cashflow-yield-discount",
                            "playbook_id": "cashflow-yield-discount",
                            "source_status": "ok",
                            "sizing_eligible": True,
                        }
                    ],
                    "metrics": {"ocf_yield": 0.11},
                }
            ],
            "provider_status_lines": [],
            "universe_exclusion_lines": [],
            "ttm_quality_counts": {},
            "evidence_hits_summary": {},
            "fallback_lines": [],
        },
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
                "INSERT INTO jquants_earnings_calendar(announcement_date, ticker) "
                "VALUES ('2026-06-10', 'AAAA')"
            )
            conn.execute(
                "INSERT INTO source_coverage(source, coverage_key, coverage_start, "
                "coverage_end, fetched_at_utc, record_count, status) "
                "VALUES ('jquants_earnings_calendar', 'legacy', '2026-05-29', "
                "'2026-08-27', '2026-05-29T00:00:00+09:00', 1, 'ok')"
            )
            conn.commit()
            conn.close()

            profile = self._build(root, "AAAA")

            events = profile["events"]
            assert isinstance(events, dict)
            self.assertIsNone(events["next_earnings_date"])

    def test_thesis_covers_price_relative_events_and_screening(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sqlite_path = root / "market.sqlite"
            # 30 bars rising 1%/day; benchmark flat; one sector peer falling.
            _insert_bars(sqlite_path, "AAAA", [100 * 1.01**i for i in range(30)], end=_ASOF)
            _insert_bars(sqlite_path, "1321", [200.0] * 30, end=_ASOF)
            _insert_bars(sqlite_path, "BBBB", [300 * 0.99**i for i in range(30)], end=_ASOF)
            _insert_reference_rows(sqlite_path)
            _write_candidates(root / "runs.sqlite")

            thesis = self._build(root, "AAAA")

            master = thesis["master"]
            assert isinstance(master, dict)
            self.assertEqual(master["sector_33"], "機械")
            price = thesis["price"]
            assert isinstance(price, dict)
            self.assertEqual(price["resolved_date"], _ASOF.isoformat())
            assert isinstance(price["return_5d"], float)
            self.assertAlmostEqual(price["return_5d"], 1.01**5 - 1, places=9)
            self.assertEqual(price["bar_count"], 30)
            self.assertAlmostEqual(price["avg_turnover_20d_oku"], 2.0, places=9)
            relative = thesis["relative"]
            assert isinstance(relative, dict)
            assert isinstance(relative["relative_5d"], float)
            self.assertAlmostEqual(relative["relative_5d"], 1.01**5 - 1, places=9)
            sector = relative["sector"]
            assert isinstance(sector, dict)
            self.assertEqual(sector["peer_count"], 1)
            assert isinstance(sector["peer_median_return_20d"], float)
            self.assertLess(sector["peer_median_return_20d"], 0)
            events = thesis["events"]
            assert isinstance(events, dict)
            self.assertEqual(events["next_earnings_date"], "2026-06-10")
            jpx = events["jpx_regulation"]
            assert isinstance(jpx, dict)
            self.assertEqual(jpx["flags"], ["特別注意銘柄"])
            screening = thesis["screening"]
            assert isinstance(screening, dict)
            self.assertTrue(screening["in_candidates"])
            entry = screening["entry"]
            assert isinstance(entry, dict)
            self.assertEqual(entry["metrics"], {"ocf_yield": 0.11})

    def test_thesis_degrades_explicitly_for_unknown_ticker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _insert_bars(root / "market.sqlite", "1321", [200.0] * 30, end=_ASOF)

            thesis = self._build(root, "ZZZZ")

            self.assertIsNone(thesis["master"])
            self.assertIsNone(thesis["price"])
            self.assertIsNone(thesis["relative"])
            screening = thesis["screening"]
            assert isinstance(screening, dict)
            self.assertFalse(screening["in_candidates"])

    def test_thesis_marks_ticker_missing_from_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sqlite_path = root / "market.sqlite"
            _insert_bars(sqlite_path, "BBBB", [300.0] * 30, end=_ASOF)
            _insert_reference_rows(sqlite_path)
            _write_candidates(root / "runs.sqlite")

            thesis = self._build(root, "BBBB")

            screening = thesis["screening"]
            assert isinstance(screening, dict)
            self.assertFalse(screening["in_candidates"])
            self.assertIn("not present", str(screening["note"]))

    def test_screening_uses_only_latest_stored_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runs_path = root / "runs.sqlite"
            _write_candidates(runs_path)
            latest = ScreeningRunStore(runs_path).publish_run(
                {
                    "run_date": "2026-05-30",
                    "asof_date": "2026-05-30",
                    "universe_size": 1,
                    "filters": {},
                    "generated_by": "test",
                    "data_sources": [],
                    "run_at": "2026-05-30T09:00:00+09:00",
                    "run_id": "screening-20260530",
                    "candidates": [
                        {
                            "ticker": "BBBB",
                            "name": "同業ペア",
                            "evidence_hits": [],
                            "metrics": {},
                        }
                    ],
                    "provider_status_lines": [],
                    "fallback_lines": [],
                }
            )

            thesis = build_ticker_profile(
                sqlite_path=root / "market.sqlite",
                ticker="AAAA",
                asof_date=date(2026, 5, 30),
                runs_db_path=runs_path,
                app_db_path=root / "app.sqlite",
            )

            screening = thesis["screening"]
            assert isinstance(screening, dict)
            self.assertEqual(screening["candidates_ref"], latest.publication_id)
            self.assertEqual(screening["candidates_asof"], "2026-05-30")
            self.assertFalse(screening["in_candidates"])

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

            current_thesis = self._build(root, "AAAA")
            old_only_thesis = self._build(root, "BBBB")

            relative = current_thesis["relative"]
            assert isinstance(relative, dict)
            sector = relative["sector"]
            assert isinstance(sector, dict)
            self.assertEqual(sector["peer_count"], 1)
            assert isinstance(sector["peer_median_return_20d"], float)
            self.assertGreater(sector["peer_median_return_20d"], 0)
            self.assertIsNone(old_only_thesis["master"])


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

    def test_command_emits_yaml_thesis(self) -> None:
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

            thesis = build_ticker_profile(
                sqlite_path=sqlite_path,
                ticker="AAAA",
                asof_date=_ASOF,
                runs_db_path=root / "runs.sqlite",
                app_db_path=root / "app.sqlite",
            )

            portfolio = thesis["portfolio"]
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
