"""JPX決算予定の合成workbookと保存HTML fixtureの共通入口。"""

from contextlib import closing
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path

import pandas as pd

from baibai_engine.data_collection.providers.jpx.models import JPXResponse
from baibai_engine.data_collection.providers.jpx.receipts import (
    complete_bundle,
    create_bundle,
    retain_bundle_response,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_sources import (
    JPX_EARNINGS_CALENDAR_INDEX_URL,
    JPX_REGULATION_URLS,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "jpx"
SOURCE_FILES = {
    "特別注意銘柄": "special_caution.html",
    "整理銘柄": "reorganization.html",
    "取引停止": "trading_halt_empty.html",
    "上場廃止警告": "delisting_warning.html",
}
FILE_A = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/kessan.xlsx"


def _workbook(stamp: str, rows: list[list[str]]) -> bytes:
    buffer = BytesIO()
    pd.DataFrame([[stamp, ""], ["決算発表予定日", "コード"], *rows]).to_excel(
        buffer, index=False, header=False
    )
    return buffer.getvalue()


DAY = date(2026, 10, 15)
STAMP = datetime(2026, 10, 15, 7, tzinfo=UTC)


def earnings(path, stamp, announcement, ticker, *, invalid=False):
    resources = (("index", JPX_EARNINGS_CALENDAR_INDEX_URL), ("workbook:0", FILE_A))
    with closing(open_connection(path)) as conn:
        bundle = create_bundle(conn, "earnings_calendar", DAY, stamp, resources)
        retain_bundle_response(
            conn,
            bundle,
            "index",
            JPXResponse(resources[0][1], stamp, "text/html", b"<a href='kessan.xlsx'>daily</a>"),
        )
        retain_bundle_response(
            conn,
            bundle,
            "workbook:0",
            JPXResponse(
                FILE_A,
                stamp,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                b"invalid" if invalid else _workbook("", [[announcement, ticker]]),
            ),
        )
        complete_bundle(conn, bundle)
    return bundle


def regulations(path, day, stamp):
    with closing(open_connection(path)) as conn:
        bundle = create_bundle(conn, "regulations", day, stamp, tuple(JPX_REGULATION_URLS.items()))
        for name, url in JPX_REGULATION_URLS.items():
            retain_bundle_response(
                conn,
                bundle,
                name,
                JPXResponse(url, stamp, "text/html", (FIXTURES / SOURCE_FILES[name]).read_bytes()),
            )
        complete_bundle(conn, bundle)
    return bundle
