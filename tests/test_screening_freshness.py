from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from baibai_loop.yaml_io import safe_load

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.freshness import (
    detect_edinet_freshness_warnings,
    load_disclosure_events,
)
from baibai_loop.screening.schema import FinancialSnapshot


def _financial(source_submit_datetime: str | None = "2025-10-15 12:00") -> FinancialSnapshot:
    return FinancialSnapshot(
        per_forward=None,
        per_trailing=None,
        pbr=None,
        ev_ebitda=None,
        p_s=None,
        pcfr=None,
        eps=None,
        sales_ttm=None,
        ocf_ttm=None,
        edinet_source_submit_datetime=source_submit_datetime,
    )


class ScreeningFreshnessTests(unittest.TestCase):
    def test_load_disclosure_events_scans_material_title_keywords(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "tdnet" / "events.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    [
                        {
                            "Code": "36780",
                            "Date": "2026-03-03",
                            "Title": "資金の借入に関するお知らせ",
                            "Source": "tdnet",
                            "URL": "https://example.test/3678-borrowing",
                        },
                        {
                            "Code": "36780",
                            "Date": "2026-03-04",
                            "Title": "定款一部変更に関するお知らせ",
                            "Source": "tdnet",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            result = load_disclosure_events(root, asof_date=date(2026, 5, 1))

            self.assertEqual(result.file_count, 1)
            self.assertEqual(result.event_count, 1)
            self.assertEqual(result.skipped_record_count, 0)
            self.assertEqual(result.unsupported_record_count, 0)
            self.assertEqual(result.load_errors, ())
            self.assertEqual(result.events_by_ticker["3678"][0].title, "資金の借入に関するお知らせ")

    @unittest.skipUnless(
        (ROOT / "records/04-candidates/2026/05/2026-05-01.yaml").is_file(),
        "weekly candidates live in the local store; skip where absent",
    )
    def test_issue_94_3678_real_candidate_gets_freshness_warnings(self) -> None:
        payload = safe_load(
            (ROOT / "records/04-candidates/2026/05/2026-05-01.yaml").read_text(encoding="utf-8")
        )
        candidate = next(item for item in payload["candidates"] if item["ticker"] == "3678")
        playbook_ids = {evidence_hit["playbook_id"] for evidence_hit in candidate["evidence_hits"]}
        self.assertLessEqual(
            {
                "valuation-reversion",
                "cashflow-yield-discount",
                "sales-discount-growth",
            },
            playbook_ids,
        )

        candidate_source_submit_datetime = _datetime_text(
            candidate["metrics"]["edinet_source_submit_datetime"]
        )
        self.assertEqual(candidate_source_submit_datetime, "2025-10-15 16:01")
        events = load_disclosure_events(
            _fixture_root(
                [
                    {"ticker": "3678", "date": "2026-03-02", "title": "持分取得に関するお知らせ"},
                    {"ticker": "3678", "date": "2026-03-03", "title": "資金の借入に関するお知らせ"},
                ]
            ),
            asof_date=date(2026, 5, 1),
        )
        warnings = detect_edinet_freshness_warnings(
            ticker="3678",
            financial=_financial(candidate_source_submit_datetime),
            events_by_ticker=events.events_by_ticker,
            asof_date=date(2026, 5, 1),
        )

        self.assertEqual(events.file_count, 1)
        self.assertEqual(events.event_count, 2)
        self.assertEqual(
            [(warning.event_date.isoformat(), warning.event_kind) for warning in warnings],
            [("2026-03-02", "m_and_a"), ("2026-03-03", "borrowing")],
        )
        self.assertEqual({warning.stale_metric for warning in warnings}, {"edinet_metrics"})

    def test_detect_edinet_freshness_warnings_includes_same_day_events(self) -> None:
        events = load_disclosure_events(
            _fixture_root(
                [
                    {"ticker": "3678", "date": "2025-10-15", "title": "資金の借入に関するお知らせ"},
                    {"ticker": "3678", "date": "2026-03-02", "title": "持分取得に関するお知らせ"},
                    {"ticker": "3678", "date": "2026-05-02", "title": "社債発行に関するお知らせ"},
                ]
            ),
            asof_date=date(2026, 5, 1),
        )

        warnings = detect_edinet_freshness_warnings(
            ticker="3678",
            financial=_financial(),
            events_by_ticker=events.events_by_ticker,
            asof_date=date(2026, 5, 1),
        )

        self.assertEqual([warning.event_kind for warning in warnings], ["borrowing", "m_and_a"])
        self.assertEqual(warnings[0].reason, "material_event_after_edinet_source")
        self.assertEqual(warnings[0].stale_metric, "edinet_metrics")

    def test_detect_edinet_freshness_warnings_classifies_non_ma_acquisition_titles(
        self,
    ) -> None:
        events = load_disclosure_events(
            _fixture_root(
                [
                    {
                        "ticker": "3678",
                        "date": "2026-03-02",
                        "title": "自己株式取得に関するお知らせ",
                    },
                    {
                        "ticker": "3678",
                        "date": "2026-03-03",
                        "title": "固定資産の取得に関するお知らせ",
                    },
                ]
            ),
            asof_date=date(2026, 5, 1),
        )

        warnings = detect_edinet_freshness_warnings(
            ticker="3678",
            financial=_financial(),
            events_by_ticker=events.events_by_ticker,
            asof_date=date(2026, 5, 1),
        )

        self.assertEqual([warning.event_kind for warning in warnings], ["share_buyback", "capex"])

    def test_load_disclosure_events_reports_bad_cache_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "broken.json").write_text("{", encoding="utf-8")
            (root / "mixed.json").write_text(
                json.dumps(
                    {
                        "events": [
                            {"ticker": "3678", "date": "2026-03-02"},
                            "not-a-record",
                            {"ticker": "3678", "date": "2026-03-03", "title": "社債発行"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (root / "unsupported.json").write_text(
                json.dumps({"events": {"ticker": "3678"}}),
                encoding="utf-8",
            )

            result = load_disclosure_events(root, asof_date=date(2026, 5, 1))

            self.assertEqual(result.file_count, 3)
            self.assertEqual(result.event_count, 1)
            self.assertEqual(result.skipped_record_count, 1)
            self.assertEqual(result.unsupported_record_count, 2)
            self.assertEqual(len(result.load_errors), 1)

    def test_detect_edinet_freshness_warnings_skips_when_source_date_is_missing(self) -> None:
        events = load_disclosure_events(
            _fixture_root([{"ticker": "3678", "date": "2026-03-02", "title": "持分取得"}]),
            asof_date=date(2026, 5, 1),
        )

        warnings = detect_edinet_freshness_warnings(
            ticker="3678",
            financial=_financial(source_submit_datetime=None),
            events_by_ticker=events.events_by_ticker,
            asof_date=date(2026, 5, 1),
        )

        self.assertEqual(warnings, ())


def _fixture_root(records: list[dict[str, str]]) -> Path:
    root = Path(tempfile.mkdtemp())
    path = root / "events.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    return root


def _datetime_text(value: object) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)
