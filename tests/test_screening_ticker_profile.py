from __future__ import annotations

import io
import tempfile
import unittest
from datetime import date
from pathlib import Path

import yaml

from baibai_loop.screening.cli import build_parser, ticker_profile_command
from baibai_loop.screening.sqlite_cache import open_connection
from baibai_loop.screening.ticker_profile import build_ticker_profile
from baibai_loop.yaml_io import safe_load
from tests.helpers.screening_sqlite import insert_daily_bars_from_closes

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
            "INSERT OR REPLACE INTO jpx_regulation_flags"
            "(asof_date, source_name, ticker, flag)"
            " VALUES ('2026-05-29', '特別注意銘柄', 'AAAA', '特別注意銘柄')"
        )
        conn.commit()
    finally:
        conn.close()


def _write_candidates(candidates_root: Path) -> None:
    path = candidates_root / "2026" / "05" / "2026-05-29.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "candidates": [
                    {
                        "ticker": "AAAA",
                        "evidence_hits": [{"name": "cashflow-yield-discount"}],
                        "metrics": {"ocf_yield": 0.11},
                    }
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


class BuildTickerProfileTests(unittest.TestCase):
    def _build(self, root: Path, ticker: str) -> dict[str, object]:
        return build_ticker_profile(
            sqlite_path=root / "market.sqlite",
            ticker=ticker,
            asof_date=_ASOF,
            candidates_root=root / "candidates",
            ledger_root=root / "records",
        )

    def test_packet_covers_price_relative_events_and_screening(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sqlite_path = root / "market.sqlite"
            # 30 bars rising 1%/day; benchmark flat; one sector peer falling.
            _insert_bars(sqlite_path, "AAAA", [100 * 1.01**i for i in range(30)], end=_ASOF)
            _insert_bars(sqlite_path, "1321", [200.0] * 30, end=_ASOF)
            _insert_bars(sqlite_path, "BBBB", [300 * 0.99**i for i in range(30)], end=_ASOF)
            _insert_reference_rows(sqlite_path)
            _write_candidates(root / "candidates")

            packet = self._build(root, "AAAA")

            master = packet["master"]
            assert isinstance(master, dict)
            self.assertEqual(master["sector_33"], "機械")
            price = packet["price"]
            assert isinstance(price, dict)
            self.assertEqual(price["resolved_date"], _ASOF.isoformat())
            assert isinstance(price["return_5d"], float)
            self.assertAlmostEqual(price["return_5d"], 1.01**5 - 1, places=9)
            self.assertEqual(price["bar_count"], 30)
            self.assertAlmostEqual(price["avg_turnover_20d_oku"], 2.0, places=9)
            relative = packet["relative"]
            assert isinstance(relative, dict)
            assert isinstance(relative["relative_5d"], float)
            self.assertAlmostEqual(relative["relative_5d"], 1.01**5 - 1, places=9)
            sector = relative["sector"]
            assert isinstance(sector, dict)
            self.assertEqual(sector["peer_count"], 1)
            assert isinstance(sector["peer_median_return_20d"], float)
            self.assertLess(sector["peer_median_return_20d"], 0)
            events = packet["events"]
            assert isinstance(events, dict)
            self.assertEqual(events["next_earnings_date"], "2026-06-10")
            jpx = events["jpx_regulation"]
            assert isinstance(jpx, dict)
            self.assertEqual(jpx["flags"], ["特別注意銘柄"])
            screening = packet["screening"]
            assert isinstance(screening, dict)
            self.assertTrue(screening["in_candidates"])
            entry = screening["entry"]
            assert isinstance(entry, dict)
            self.assertEqual(entry["metrics"], {"ocf_yield": 0.11})

    def test_packet_degrades_explicitly_for_unknown_ticker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _insert_bars(root / "market.sqlite", "1321", [200.0] * 30, end=_ASOF)

            packet = self._build(root, "ZZZZ")

            self.assertIsNone(packet["master"])
            self.assertIsNone(packet["price"])
            self.assertIsNone(packet["relative"])
            screening = packet["screening"]
            assert isinstance(screening, dict)
            self.assertFalse(screening["in_candidates"])

    def test_packet_marks_ticker_missing_from_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sqlite_path = root / "market.sqlite"
            _insert_bars(sqlite_path, "BBBB", [300.0] * 30, end=_ASOF)
            _insert_reference_rows(sqlite_path)
            _write_candidates(root / "candidates")

            packet = self._build(root, "BBBB")

            screening = packet["screening"]
            assert isinstance(screening, dict)
            self.assertFalse(screening["in_candidates"])
            self.assertIn("not present", str(screening["note"]))


class TickerProfileCliTests(unittest.TestCase):
    def test_parser_accepts_ticker_profile_arguments(self) -> None:
        args = build_parser().parse_args(
            ["ticker-profile", "--ticker", "8303", "--asof", "2026-06-08"]
        )
        self.assertEqual(args.ticker, "8303")
        self.assertEqual(args.asof, "2026-06-08")
        self.assertTrue(args.sqlite_path.endswith("market.sqlite"))

    def test_command_rejects_invalid_ticker(self) -> None:
        exit_code = ticker_profile_command(
            ticker="not-a-ticker",
            asof="2026-06-08",
            sqlite_path=Path("/nonexistent.sqlite"),
            candidates_root=Path("/nonexistent"),
            ledger_root=Path("/nonexistent"),
            stdout=io.StringIO(),
        )
        self.assertEqual(exit_code, 1)

    def test_command_emits_yaml_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sqlite_path = root / "market.sqlite"
            _insert_bars(sqlite_path, "AAAA", [100.0] * 30, end=_ASOF)
            buffer = io.StringIO()
            exit_code = ticker_profile_command(
                ticker="AAAA",
                asof=_ASOF.isoformat(),
                sqlite_path=sqlite_path,
                candidates_root=root / "candidates",
                ledger_root=root / "records",
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
            trade_dir = root / "records" / "06-trades" / "2026" / "05"
            trade_dir.mkdir(parents=True)
            (trade_dir / "2026-05-13-bbbb.md").write_text(
                "---\n"
                "trade_id: trade-1\n"
                "ticker: 'BBBB'\n"
                "name: 同業ペア\n"
                "position_state: open\n"
                "review_state: scheduled\n"
                "executions:\n"
                "- execution_id: exec-1\n"
                "  side: buy\n"
                "  quantity: 100\n"
                "  price_yen: 500\n"
                "  at: '2026-05-14T09:00:00+09:00'\n"
                "---\n",
                encoding="utf-8",
            )

            packet = build_ticker_profile(
                sqlite_path=sqlite_path,
                ticker="AAAA",
                asof_date=_ASOF,
                candidates_root=root / "candidates",
                ledger_root=root / "records",
            )

            portfolio = packet["portfolio"]
            assert isinstance(portfolio, dict)
            self.assertEqual(portfolio["open_position_count"], 1)
            self.assertFalse(portfolio["holds_this_ticker"])
            # BBBB shares AAAA's sector (機械) so the whole notional concentrates there.
            self.assertEqual(portfolio["same_sector_position_count"], 1)
            self.assertEqual(portfolio["same_sector_entry_notional_share"], 1.0)
            positions = portfolio["open_positions"]
            assert isinstance(positions, list)
            self.assertEqual(positions[0]["entry_notional_yen"], 50000)
