"""保存済みEDINET抜粋を束ねる入力。実取得やテストmoduleのimportを行わない。"""

import io
import zipfile
from pathlib import Path

from baibai_engine.machine_analysis.observations.financials.edinet_facts.extract import (
    extract_facts,
)

FIXTURES = Path(__file__).parents[1] / "fixtures/edinet/research"


def source_zip(doc_id="S100YR5P", replace=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in FIXTURES.glob(doc_id + "*"):
            text = path.read_text()
            for old, new in (replace or {}).items():
                text = text.replace(old, new)
            archive.writestr("XBRL/PublicDoc/" + path.name, text)
    return buffer.getvalue()


def facts(doc_id="S100YR5P", replace=None):
    return extract_facts(
        content=source_zip(doc_id, replace),
        ticker="1873",
        doc_id=doc_id,
        submitted="2026-07-22 10:00",
    )
