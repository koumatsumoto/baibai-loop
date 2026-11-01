"""財務省の案内と別受領本文を混同しない合成回帰。"""

import pytest

from baibai_engine.data_collection.providers.articles.native import (
    NativeArticleUnsupported,
    discover_links,
    parse_document,
    parser_revision_for,
)

NOTICE = "https://www.mof.go.jp/insideLink/synthetic.html"
TARGET = "https://www.mof.go.jp/pri/research/synthetic.htm"


def notice(target=TARGET, delay="0"):
    return f'<meta http-equiv="refresh" content="{delay};url={target}" />'.encode()


@pytest.mark.parametrize("target", [TARGET, "https://lfb.mof.go.jp/renkei/torikumi.html"])
def test_notice_retains_explicit_target_without_claiming_body_or_publication_date(target):
    item = parse_document("mof_news", notice(target), url=NOTICE, title="案内の見出し")
    assert item.url == NOTICE
    assert item.title == "案内の見出し"
    assert item.content_scope == "unavailable"
    assert item.text == ""
    assert item.published_raw is None
    assert item.metadata == {"redirect_target_url": target}
    assert item.missing_parts == ("redirect_target_body_is_a_separate_receipt",)
    assert parser_revision_for("mof_news") == "article-native-mof-v6"


@pytest.mark.parametrize(
    "target",
    [
        "https://example.com/document.htm",
        "https://unlisted.mof.go.jp/document.htm",
        "https://lfb.mof.go.jp.example.com/document.htm",
        "http://www.mof.go.jp/document.htm",
        "http://lfb.mof.go.jp/document.htm",
        "https://user@www.mof.go.jp/document.htm",
        "https://www.mof.go.jp:8443/document.htm",
        "https://www.mof.go.jp/insideLink/next.html",
        "https://lfb.mof.go.jp/insideLink/next.html",
        "https://www.mof.go.jp/outside_link/next.html",
        "https://www.customs.go.jp/another-page.htm",
        "https://www.customs.go.jp/toukei/latest/index.htm?alternate=1",
        NOTICE,
        "javascript:alert(1)",
    ],
)
def test_notice_does_not_expand_host_scheme_credentials_port_or_redirect_chain(target):
    with pytest.raises(NativeArticleUnsupported):
        parse_document("mof_news", notice(target), url=NOTICE)


def test_customs_notice_is_a_separate_bounded_link_and_listing_does_not_expand_history():
    origin = "https://www.mof.go.jp/outside_link/synthetic.html"
    target = "https://www.customs.go.jp/toukei/latest/index.htm"
    links = discover_links("mof_news", notice(target), url=origin)
    assert [(item.locator, item.url) for item in links] == [("/html/meta-refresh", target)]
    item = parse_document("mof_news", notice(target), url=origin)
    assert item.content_scope == "unavailable"
    body = b'<div id="main"><h1>Latest</h1><a href="../old.pdf">Old</a></div>'
    assert discover_links("mof_news", body, url=target) == ()
    projected = parse_document("mof_news", body, url=target)
    assert projected.metadata["attachments"] == ["https://www.customs.go.jp/toukei/old.pdf"]
    assert projected.metadata["attachment_scope"] == "rolling_listing_reference_only"
    assert projected.published_raw is None


def test_link_discovery_does_not_require_pdf_body_projection():
    assert discover_links("mof_news", b"%PDF-not-extractable", url=TARGET) == ()


@pytest.mark.parametrize(
    "body",
    [notice(delay="5"), notice() + notice(), b'<meta http-equiv="refresh" content="invalid">'],
)
def test_ambiguous_or_delayed_notice_remains_unsupported(body):
    with pytest.raises(NativeArticleUnsupported):
        parse_document("mof_news", body, url=NOTICE)


def test_other_sources_and_non_notice_paths_do_not_gain_redirect_following():
    with pytest.raises(NativeArticleUnsupported):
        parse_document("boj_news", notice(), url=NOTICE)
    with pytest.raises(NativeArticleUnsupported):
        parse_document("mof_news", notice(), url=TARGET)


def test_pri_body_excludes_site_heading_navigation_and_footer():
    html = (
        '<header><h1>サイト名</h1></header><div class="body_wrap">'
        '<p id="navi">パンくず</p><div id="contents-wrap"><h2>講演会一覧</h2>'
        '<p>合成の本文</p><a href="synthetic.pdf">資料</a></div></div>'
        "<footer>フッター</footer>"
    ).encode("shift_jis")
    item = parse_document("mof_news", html, url=TARGET, encoding="shift_jis")
    assert item.title == "講演会一覧"
    assert "合成の本文" in item.text
    assert all(text not in item.text for text in ["サイト名", "パンくず", "フッター"])
    assert item.content_scope == "partial_text"
    assert item.metadata["attachments"] == ["https://www.mof.go.jp/pri/research/synthetic.pdf"]
    assert item.published_raw is None
    with pytest.raises(NativeArticleUnsupported):
        parse_document("mof_news", html + html, url=TARGET, encoding="shift_jis")


def test_bureau_body_uses_its_native_region_without_broadening_other_hosts():
    body = (
        "<header><h1>サイト名</h1></header><nav>共通ナビ</nav>"
        '<div id="mainArea"><h1>合成の財務局資料</h1><p>合成本文</p></div>'
        "<footer>共通フッター</footer>"
    ).encode()
    target = "https://lfb.mof.go.jp/renkei/torikumi.html"
    item = parse_document("mof_news", body, url=target)
    assert item.title == "合成の財務局資料"
    assert item.content_scope == "partial_text"
    assert "合成本文" in item.text
    assert all(text not in item.text for text in ("サイト名", "共通ナビ", "共通フッター"))
    assert item.published_raw is None
    with pytest.raises(NativeArticleUnsupported):
        parse_document("mof_news", body + body, url=target)
    with pytest.raises(NativeArticleUnsupported):
        parse_document("mof_news", body, url=TARGET)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv",
        "https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv",
    ],
)
def test_mof_yield_csv_preserves_native_text_without_interpreting_values(url):
    text = '合成金利表,,単位%\r\n基準日,1年,2年\r\nR8.1.5,0.5,""\r\n注記,,\r\n'
    item = parse_document("mof_news", text.encode("cp932"), url=url, encoding="utf-8")
    assert item.text == text
    assert item.locator == "/csv"
    assert item.title == "合成金利表"
    assert item.content_scope == "full_text"
    assert item.published_raw is None
    assert item.metadata == {"format": "csv", "encoding": "cp932", "row_count": 4}
    assert "attachments" not in item.metadata


@pytest.mark.parametrize("text", ["not a yield table", "基準日,1年\n基準日,2年\n"])
def test_known_csv_url_does_not_make_invalid_body_a_success(text):
    with pytest.raises(NativeArticleUnsupported):
        parse_document(
            "mof_news",
            text.encode("cp932"),
            url="https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv",
        )


def test_other_csv_routes_do_not_gain_mof_table_interpretation():
    body = "基準日,1年\nR8.1.5,0.5\n".encode("cp932")
    with pytest.raises(NativeArticleUnsupported):
        parse_document("mof_news", body, url=TARGET, encoding="cp932")


def protected_pdf(password="", pages=1):
    from io import BytesIO

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=400, height=400)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 12 Tf 20 300 Td (Synthetic procurement answer) Tj ET")
        page[NameObject("/Contents")] = content
    writer.encrypt(user_password=password, owner_password="synthetic-owner")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_mof_public_pdf_with_encryption_dictionary_is_read_without_password():
    from io import BytesIO

    from pypdf import PdfReader

    body = protected_pdf()
    assert PdfReader(BytesIO(body)).is_encrypted
    item = parse_document("mof_news", body, url="https://www.mof.go.jp/synthetic.pdf")
    assert "Synthetic procurement answer" in item.text
    assert item.content_scope == "partial_text"
    assert item.missing_parts == ("non_text_figures_and_layout",)
    assert item.metadata["page_spans"] == [{"page": 1, "start": 0, "end": len(item.text)}]
    with pytest.raises(ValueError, match="encrypted"):
        parse_document("boj_news", body, url="https://www.boj.or.jp/synthetic.pdf")


def test_mof_pdf_requiring_password_or_over_page_limit_is_still_rejected():
    from pypdf.errors import PdfReadError

    with pytest.raises(PdfReadError):
        parse_document("mof_news", protected_pdf("required"), url=TARGET)
    with pytest.raises(ValueError, match="oversized"):
        parse_document("mof_news", protected_pdf(pages=101), url=TARGET)


def test_mof_pdf_reprojection_preserves_original_and_prior_failure(tmp_path):
    import hashlib
    from contextlib import closing
    from datetime import UTC, datetime

    from baibai_engine.data_collection.receipts.articles.contracts import ArticleReceipt, BodyRef
    from baibai_engine.data_collection.receipts.articles.store import read_body, retain_receipt
    from baibai_engine.machine_analysis.observations.articles.contracts import ArticleProjectionRun
    from baibai_engine.machine_analysis.observations.articles.projection import project_receipt
    from baibai_engine.machine_analysis.observations.articles.store import save_projection
    from baibai_engine.storage.sqlite.source_cache import schema

    body = protected_pdf()
    now = datetime(2026, 11, 1, tzinfo=UTC)
    receipt = ArticleReceipt(
        receipt_id="mof-protected-pdf",
        source="mof_news",
        requested_url="https://www.mof.go.jp/synthetic.pdf",
        received_at=now,
        status="ok",
        http_status=200,
        body_ref=BodyRef(receipt_id="mof-protected-pdf", sha256=hashlib.sha256(body).hexdigest()),
    )
    with closing(schema.open_connection(tmp_path / "market.sqlite")) as conn, conn:
        retain_receipt(conn, receipt, body=body)
        old = ArticleProjectionRun(
            receipt_id=receipt.receipt_id,
            receipt_sha256=receipt.sha256,
            parser_revision="article-native-mof-v3",
            generated_at=now,
            status="parse_failed",
            error_code="ValueError",
        )
        save_projection(conn, old, ())
        new, (item,) = project_receipt(conn, receipt.receipt_id, kind="document")
        assert new.status == "parsed"
        assert new.parser_revision == "article-native-mof-v6"
        assert "Synthetic procurement answer" in item.text
        assert read_body(conn, receipt.body_ref) == body
        repeated, _ = project_receipt(conn, receipt.receipt_id, kind="document")
        assert repeated == new
        assert conn.execute("SELECT COUNT(*) FROM article_projection_runs").fetchone()[0] == 2
