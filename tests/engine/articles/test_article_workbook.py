"""合成 XLSX の有限回帰。外部取得や公開原文の転載は行わない。"""

import hashlib
import io
import zipfile
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import openpyxl
import pytest

from baibai_engine.data_collection.providers.articles import workbook as parser
from baibai_engine.data_collection.providers.articles.native import (
    parse_document,
    parser_revision_for,
)
from baibai_engine.data_collection.receipts.articles.contracts import ArticleReceipt, BodyRef
from baibai_engine.data_collection.receipts.articles.store import retain_receipt
from baibai_engine.machine_analysis.observations.articles.contracts import ArticleProjectionRun
from baibai_engine.machine_analysis.observations.articles.projection import project_receipt
from baibai_engine.machine_analysis.observations.articles.store import save_projection
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

URL = "https://www.boj.or.jp/synthetic.xlsx"


def workbook_bytes(*, empty=False):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "統計"
    if not empty:
        sheet.append(["項目", "当期", "比較期"])
        sheet.append(["数量", 0, 12.5])
        sheet.append([date(2026, 10, 31), False, "=B2+C2"])
        other = book.create_sheet("注記")
        other["B2"] = "注記\n原文"
    output = io.BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


def rewritten_archive(body, name, replacement):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(body)) as original, zipfile.ZipFile(output, "w") as target:
        for entry in original.infolist():
            text = original.read(entry.filename)
            target.writestr(entry.filename, replacement(text) if entry.filename == name else text)
    return output.getvalue()


def test_boj_workbook_cells_keep_coordinates_zero_dates_and_uncomputed_formulas():
    article = parse_document("boj_news", workbook_bytes(), url=URL, title="公表資料")
    assert article.title == "公表資料"
    assert article.locator == "/xlsx/cells"
    assert article.content_scope == "partial_text"
    assert "統計!B2\t0" in article.text
    assert "統計!C2\t12.5" in article.text
    assert "統計!A3\t2026-10-31T00:00:00" in article.text
    assert "統計!B3\tFalse" in article.text
    assert "統計!C3\t=B2+C2" in article.text
    assert "注記!B2\t注記\n原文" in article.text
    for span in article.metadata["cell_spans"]:
        assert article.text[span["start"] : span["end"]].startswith(
            f"{span['sheet']}!{span['cell']}\t"
        )
    assert article.missing_parts == ("formatting_charts_images_and_formula_evaluation",)
    assert parser_revision_for("boj_news") == "article-native-boj-v2"
    assert parser_revision_for("mof_news") == "article-native-v1"


def test_incorrect_dimension_does_not_silently_drop_cells():
    body = rewritten_archive(
        workbook_bytes(),
        "xl/worksheets/sheet1.xml",
        lambda text: text.replace(b'ref="A1:C3"', b'ref="A1:A1"'),
    )
    assert "統計!C3\t=B2+C2" in parse_document("boj_news", body, url=URL).text


def test_workbook_limits_fail_without_partial_success(monkeypatch):
    monkeypatch.setattr(parser, "MAX_WORKBOOK_XML_BYTES", 1)
    with pytest.raises(ValueError, match="expanded size limit"):
        parse_document("boj_news", workbook_bytes(), url=URL)
    monkeypatch.setattr(parser, "MAX_WORKBOOK_XML_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(parser, "MAX_WORKBOOK_CELLS", 2)
    with pytest.raises(ValueError, match="cell limit"):
        parse_document("boj_news", workbook_bytes(), url=URL)


def test_empty_workbook_is_not_empty_success():
    with pytest.raises(ValueError, match="no readable cells"):
        parse_document("boj_news", workbook_bytes(empty=True), url=URL)


def test_zip_that_is_not_workbook_is_not_html_or_readable_cells():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("unrelated.txt", "unrelated source")
    with pytest.raises(ValueError, match="not an XLSX"):
        parse_document("boj_news", output.getvalue(), url=URL)
    with pytest.raises(ValueError, match="invalid"):
        parse_document("boj_news", b"PK\x03\x04broken", url=URL)


def test_invalid_workbook_xml_remains_failed():
    body = rewritten_archive(workbook_bytes(), "xl/workbook.xml", lambda _: b"<invalid")
    with pytest.raises(ValueError, match="invalid"):
        parse_document("boj_news", body, url=URL)


def test_reprojection_preserves_failed_history_and_reuses_new_result(tmp_path):
    body = workbook_bytes()
    stamp = datetime(2026, 10, 31, tzinfo=UTC)
    receipt = ArticleReceipt(
        receipt_id="synthetic-workbook",
        source="boj_news",
        requested_url=URL,
        received_at=stamp,
        status="ok",
        http_status=200,
        body_ref=BodyRef(receipt_id="synthetic-workbook", sha256=hashlib.sha256(body).hexdigest()),
    )
    with closing(open_connection(tmp_path / "market.sqlite")) as conn, conn:
        retain_receipt(conn, receipt, body=body)
        old = ArticleProjectionRun(
            receipt_id=receipt.receipt_id,
            receipt_sha256=receipt.sha256,
            parser_revision="article-native-v1",
            generated_at=stamp,
            status="parse_failed",
            error_code="UnicodeDecodeError",
        )
        save_projection(conn, old, ())
        new, observations = project_receipt(
            conn, receipt.receipt_id, kind="document", generated_at=stamp + timedelta(minutes=1)
        )
        assert new.status == "parsed"
        assert new.parser_revision == "article-native-boj-v2"
        assert len(observations) == 1
        replay, repeated = project_receipt(
            conn, receipt.receipt_id, kind="document", generated_at=stamp + timedelta(hours=1)
        )
        assert replay == new
        assert repeated == observations
        assert conn.execute("SELECT COUNT(*) FROM article_projection_runs").fetchone()[0] == 2
        assert (
            conn.execute(
                "SELECT run_json FROM article_projection_runs WHERE run_id=?", (old.run_id,)
            ).fetchone()[0]
            == old.model_dump_json()
        )
