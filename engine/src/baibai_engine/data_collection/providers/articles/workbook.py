"""保存済み XLSX のセル文字列と座標。計算式や経済的な意味は評価しない。"""

from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, time

import openpyxl
from openpyxl.utils.exceptions import InvalidFileException
from pydantic import JsonValue

from baibai_engine.data_collection.providers.articles.contracts import NativeArticle

MAX_WORKBOOK_XML_BYTES = 64 * 1024 * 1024
MAX_WORKBOOK_CELLS = 100_000


def parse_workbook(body: bytes, *, source: str, url: str, title: str) -> NativeArticle:
    """Extract explicit cells, preserving zero, formulas, dates and source coordinates."""
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names = set(archive.namelist())
            if not {"[Content_Types].xml", "xl/workbook.xml"} <= names:
                raise ValueError("article ZIP is not an XLSX workbook")
            if sum(item.file_size for item in archive.infolist()) > MAX_WORKBOOK_XML_BYTES:
                raise ValueError("article workbook expanded size limit")
        workbook = openpyxl.load_workbook(
            io.BytesIO(body), read_only=True, data_only=False, keep_links=False
        )
        try:
            parts: list[str] = []
            spans: list[JsonValue] = []
            offset = 0
            cells = 0
            for sheet in workbook.worksheets:
                # Producer dimensions can omit populated cells. Read the actual XML,
                # bounding visited rows/cells, including empty gaps, instead.
                sheet.reset_dimensions()
                for row in sheet.iter_rows():
                    cells += max(1, len(row))
                    if cells > MAX_WORKBOOK_CELLS:
                        raise ValueError("article workbook cell limit")
                    for cell in row:
                        value = cell.value
                        if value is None:
                            continue
                        if isinstance(value, (datetime, date, time)):
                            text = value.isoformat()
                        else:
                            text = str(value)
                        if not text:
                            continue
                        part = f"{sheet.title}!{cell.coordinate}\t{text}"
                        parts.append(part)
                        spans.append(
                            {
                                "sheet": sheet.title,
                                "cell": cell.coordinate,
                                "cell_type": cell.data_type,
                                "start": offset,
                                "end": offset + len(part),
                            }
                        )
                        offset += len(part) + 1
        finally:
            workbook.close()
    except (zipfile.BadZipFile, InvalidFileException, SyntaxError, KeyError) as exc:
        raise ValueError("article XLSX is invalid") from exc
    if not parts:
        raise ValueError("article workbook has no readable cells")
    return NativeArticle(
        article_id=f"{source}:{url}",
        locator="/xlsx/cells",
        url=url,
        title=title,
        text="\n".join(parts),
        content_scope="partial_text",
        missing_parts=("formatting_charts_images_and_formula_evaluation",),
        metadata={"format": "xlsx_cells", "cell_spans": spans},
    )
