"""財務省の案内と別受領本文を混同しない合成回帰。"""

import pytest

from baibai_engine.data_collection.providers.articles.native import (
    NativeArticleUnsupported,
    parse_document,
    parser_revision_for,
)

NOTICE = "https://www.mof.go.jp/insideLink/synthetic.html"
TARGET = "https://www.mof.go.jp/pri/research/synthetic.htm"


def notice(target=TARGET, delay="0"):
    return f'<meta http-equiv="refresh" content="{delay};url={target}" />'.encode()


def test_notice_retains_explicit_target_without_claiming_body_or_publication_date():
    item = parse_document("mof_news", notice(), url=NOTICE, title="案内の見出し")
    assert item.url == NOTICE
    assert item.title == "案内の見出し"
    assert item.content_scope == "unavailable"
    assert item.text == ""
    assert item.published_raw is None
    assert item.metadata == {"redirect_target_url": TARGET}
    assert item.missing_parts == ("redirect_target_body_is_a_separate_receipt",)
    assert parser_revision_for("mof_news") == "article-native-mof-v2"


@pytest.mark.parametrize(
    "target",
    [
        "https://example.com/document.htm",
        "http://www.mof.go.jp/document.htm",
        "https://user@www.mof.go.jp/document.htm",
        "https://www.mof.go.jp:8443/document.htm",
        "https://www.mof.go.jp/insideLink/next.html",
        NOTICE,
        "javascript:alert(1)",
    ],
)
def test_notice_does_not_expand_host_scheme_credentials_port_or_redirect_chain(target):
    with pytest.raises(NativeArticleUnsupported):
        parse_document("mof_news", notice(target), url=NOTICE)


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
        '<footer>フッター</footer>'
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
