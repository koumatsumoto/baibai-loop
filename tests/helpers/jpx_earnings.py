"""JPX決算予定の合成workbookと保存HTML fixtureの共通入口。"""

from io import BytesIO
from pathlib import Path

import pandas as pd

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "jpx"
SOURCE_FILES = {
    "特別注意銘柄": "special_caution.html",
    "整理銘柄": "reorganization.html",
    "取引停止": "trading_halt_empty.html",
    "上場廃止警告": "delisting_warning.html",
}
FILE_A = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/kessan.xlsx"
FILE_B = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/kessan09_1015.xlsx"


def _workbook(stamp: str, rows: list[list[str]]) -> bytes:
    buffer = BytesIO()
    pd.DataFrame([[stamp, ""], ["決算発表予定日", "コード"], *rows]).to_excel(
        buffer, index=False, header=False
    )
    return buffer.getvalue()
