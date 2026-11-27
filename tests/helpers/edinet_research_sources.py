"""保存済みEDINET抜粋を束ねる入力。実取得やテストmoduleのimportを行わない。"""

import io
import zipfile
from pathlib import Path

from baibai_engine.machine_analysis.observations.financials.edinet_facts.extract import (
    extract_facts,
)

FIXTURES = Path(__file__).parents[1] / "fixtures/edinet/research"
_SOURCE_TICKERS = {
    "S100XTM0": "2501",
    "S100YCUF": "5410",
    "S100YJ24": "1811",
    "S100YR5P": "1873",
}
_TEST_SUBMITTED = "2026-07-22 10:00"


def source_zip(doc_id="S100YR5P", replace=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in FIXTURES.glob(doc_id + "*"):
            text = path.read_text(encoding="utf-8")
            for old, new in (replace or {}).items():
                text = text.replace(old, new)
            archive.writestr("XBRL/PublicDoc/" + path.name, text)
    return buffer.getvalue()


def facts_at_test_clock(doc_id="S100YR5P", replace=None, *, synthetic_ticker=None):
    """実docIDには出典の銘柄を使う。同一銘柄の仮想履歴だけ明示的に上書きする。"""
    ticker = _SOURCE_TICKERS[doc_id] if synthetic_ticker is None else synthetic_ticker
    return extract_facts(
        content=source_zip(doc_id, replace),
        ticker=ticker,
        doc_id=doc_id,
        submitted=_TEST_SUBMITTED,
    )
