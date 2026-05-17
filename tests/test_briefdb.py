from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

import yaml

from baibai_loop.briefdb.cli import main
from baibai_loop.briefdb.coverage import check_coverage
from baibai_loop.briefdb.db import SQLITE_SCHEMA_VERSION, initialize_database
from baibai_loop.briefdb.generate import generate_weekly
from baibai_loop.briefdb.migrate import migrate_briefs


class BriefDBTest(unittest.TestCase):
    def test_init_creates_schema_and_seed_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"
            conn = initialize_database(db)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                requirement_count = conn.execute(
                    "SELECT COUNT(*) FROM coverage_requirements WHERE kind = 'world-weekly'"
                ).fetchone()[0]
            finally:
                conn.close()

            self.assertEqual(version, SQLITE_SCHEMA_VERSION)
            self.assertGreater(requirement_count, 0)

    def test_migrate_briefs_loads_numeric_items_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_weekly_brief(root, "2026-05-10", _current_values())
            db = root / "macro.sqlite"

            result = migrate_briefs(root, db)

            conn = sqlite3.connect(db)
            try:
                observation_count = conn.execute(
                    "SELECT COUNT(*) FROM indicator_observations"
                ).fetchone()[0]
                has_events_table = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'events'"
                ).fetchone()[0]
            finally:
                conn.close()

            self.assertEqual(result.brief_files, 1)
            self.assertEqual(observation_count, 9)
            self.assertEqual(has_events_table, 0)

    def test_coverage_and_generate_weekly_after_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_weekly_brief(root, "2026-05-03", _previous_values())
            _write_weekly_brief(root, "2026-05-10", _current_values())
            db = root / "macro.sqlite"
            migrate_briefs(root, db)

            result = check_coverage(
                db,
                kind="world-weekly",
                start=_date("2026-05-04"),
                end=_date("2026-05-10"),
            )
            payload = generate_weekly(
                db,
                start=_date("2026-05-04"),
                end=_date("2026-05-10"),
            )

            self.assertTrue(result.ok)
            self.assertIn(
                "WTI原油",
                {item["name"] for item in payload["layers"]["world"]["market_indicators"]},
            )
            self.assertIn("USD/JPY", {item["name"] for item in payload["layers"]["japan"]["fx"]})
            self.assertIn(
                "WTI原油",
                {item["indicator"] for item in payload["deltas"]["threshold_breaches"]},
            )

    def test_coverage_fails_without_required_observations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"
            initialize_database(db).close()

            result = check_coverage(
                db,
                kind="world-weekly",
                start=_date("2026-05-04"),
                end=_date("2026-05-10"),
            )

            self.assertFalse(result.ok)
            self.assertTrue(
                any("missing ok observation" in item.message for item in result.findings)
            )

    def test_cli_migrate_and_generate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_weekly_brief(root, "2026-05-03", _previous_values())
            _write_weekly_brief(root, "2026-05-10", _current_values())
            db = root / "macro.sqlite"

            self.assertEqual(main(["migrate-briefs", "--root", str(root), "--db", str(db)]), 0)
            self.assertEqual(
                main(
                    [
                        "generate-weekly",
                        "--db",
                        str(db),
                        "--start",
                        "2026-05-04",
                        "--end",
                        "2026-05-10",
                    ]
                ),
                0,
            )


def _write_weekly_brief(root: Path, observation_date: str, values: dict[str, str]) -> None:
    path = root / "records/02-brief/2026/05" / f"{observation_date}-world-weekly-test.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    current = observation_date == "2026-05-10"
    rates_date = "2026-05-07" if current else "2026-05-02"
    vix_date = "2026-05-08" if current else "2026-05-02"
    oil_date = "2026-05-04" if current else "2026-04-25"
    brent_date = "2026-05-01" if current else "2026-04-25"
    fx_date = "2026-05-08" if current else "2026-05-02"
    payload = {
        "schema_version": 1,
        "kind": "world-weekly",
        "type": "periodic",
        "scope": "world",
        "ai_draft": True,
        "published_at": f"{observation_date}T18:00:00+09:00",
        "observation_date": observation_date,
        "period": {"start": "2026-05-04", "end": observation_date},
        "sources": [
            {
                "id": "fed-h15",
                "name": "Federal Reserve H.15",
                "url": "https://www.federalreserve.gov/releases/h15/",
                "accessed_at": observation_date,
                "status": "ok",
            },
            {
                "id": "cboe-vix",
                "name": "CBOE VIX",
                "url": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
                "accessed_at": observation_date,
                "status": "ok",
            },
            {
                "id": "eia-brent",
                "name": "EIA Brent",
                "url": "https://www.eia.gov/dnav/pet/hist/RBRTEd.htm",
                "accessed_at": observation_date,
                "status": "ok",
            },
            {
                "id": "eia-wti",
                "name": "EIA WTI",
                "url": "https://www.eia.gov/dnav/pet/hist/RWTCd.htm",
                "accessed_at": observation_date,
                "status": "ok",
            },
            {
                "id": "ecb-fx",
                "name": "ECB FX",
                "url": "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip",
                "accessed_at": observation_date,
                "status": "ok",
            },
        ],
        "layers": {
            "world": {
                "market_indicators": [
                    _indicator("米10Y利回り", values["us10y"], rates_date, "fed-h15"),
                    _indicator("米2Y利回り", values["us2y"], rates_date, "fed-h15"),
                    _indicator("10Y-2Yスプレッド", values["spread"], rates_date, "fed-h15"),
                    _indicator("VIX", values["vix"], vix_date, "cboe-vix"),
                    _indicator("Brent原油", values["brent"], brent_date, "eia-brent"),
                    _indicator("WTI原油", values["wti"], oil_date, "eia-wti"),
                ],
                "events": [{"date": observation_date, "text": "not migrated", "source_ids": []}],
            },
            "japan": {
                "fx": [
                    _indicator("USD/JPY", values["usdjpy"], fx_date, "ecb-fx"),
                    _indicator("EUR/JPY", values["eurjpy"], fx_date, "ecb-fx"),
                    _indicator("AUD/JPY", values["audjpy"], fx_date, "ecb-fx"),
                ]
            },
            "japan_equity": {},
        },
        "deltas": {"threshold_breaches": [], "direction_history": []},
        "fact_memos": ["not migrated"],
        "next_events": [],
        "notes": "not migrated",
    }
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _indicator(name: str, value: str, as_of: str, source_id: str) -> dict[str, object]:
    return {"name": name, "value": value, "as_of": as_of, "source_ids": [source_id]}


def _previous_values() -> dict[str, str]:
    return {
        "us10y": "4.40%",
        "us2y": "3.88%",
        "spread": "+52bp",
        "vix": "16.99",
        "brent": "$113.89",
        "wti": "$99.89",
        "usdjpy": "156.56",
        "eurjpy": "183.21",
        "audjpy": "111.91",
    }


def _current_values() -> dict[str, str]:
    return {
        "us10y": "4.41%",
        "us2y": "3.92%",
        "spread": "+49bp",
        "vix": "17.19",
        "brent": "$118.26",
        "wti": "$109.76",
        "usdjpy": "156.76",
        "eurjpy": "184.37",
        "audjpy": "113.40",
    }


def _date(value: str) -> date:
    return date.fromisoformat(value)
