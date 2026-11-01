"""PDF財務の有限合成本文。実PDFの受入証拠として流用しない。"""

from __future__ import annotations

from datetime import UTC, date, datetime

from baibai_engine.machine_analysis.observations.financials.pdf_statements import (
    PdfStatement,
    extract_pages,
)

STAMP = datetime(2026, 11, 1, tzinfo=UTC)
ASOF = date(2026, 10, 31)
URL = "https://www2.jpx.co.jp/disc/92680/140120260804508616.pdf"
FY_URL = "https://www2.jpx.co.jp/disc/92680/140120260511521258.pdf"


def pages(annual=False):
    if annual:
        period = "前連結会計年度\n2024年4月1日\n2025年3月31日\n当連結会計年度\n2025年4月1日\n2026年3月31日"
        table = "(連結損益計算書)"
        heading = "2026年3月期 決算短信〔IFRS〕(連結)\n2026年5月15日"
        headline = "2026年3月期 315,507 17.3 9,837 10.5 4,496 53.9 2,719 49.9 2,469 89.2 8,611 900.2\n2025年3月期 268,825 - 8,900 - 2,919 - 1,813 - 1,305 - 860 -"
        amounts = "売上高 268,825 315,507\n営業利益 8,900 9,837\n親会社の所有者 1,305 2,469"
        basis = "前連結会計年度の数値についても、IFRSベースに組み替えて比較分析"
    else:
        period = "前第1四半期連結累計期間\n2025年4月1日\n2025年6月30日\n当第1四半期連結累計期間\n2026年4月1日\n2026年6月30日"
        table = "(要約四半期連結損益計算書)"
        heading = "2027年3月期 第1四半期決算短信〔IFRS〕(連結)\n2026年8月7日"
        headline = "2027年3月期第1四半期 106,641 54.3 3,528 78.3 1,369 89.5 982 129.5 924 152.6 1,529 60.1\n2026年3月期第1四半期 69,111 - 1,979 - 722 - 428 - 366 - 955 -"
        amounts = "売上高 69,111 106,641\n営業利益 1,979 3,528\n親会社の所有者 366 924"
        basis = "前第1四半期連結累計期間の数値についても、IFRS会計基準ベースに組み替えて比較分析"
    return [
        heading
        + "\n上場会社名\nコード番号 9268\n(百万円未満切捨て)\n"
        + headline
        + "\n基本的1株当たり",
        basis,
        table + "\n(単位:百万円)\n" + period + "\n" + amounts,
    ]


def base(annual=False):
    return PdfStatement(
        ticker="9268",
        disclosure_number="20260511521258" if annual else "20260804508616",
        source_url=FY_URL if annual else URL,
        source_sha256=("a" if annual else "b") * 64,
        bundle_id="synthetic",
        resource_name="pdf:20260511521258" if annual else "pdf:20260804508616",
        received_at=STAMP,
    )


def statement(annual=False):
    return extract_pages(base(annual), pages(annual))
