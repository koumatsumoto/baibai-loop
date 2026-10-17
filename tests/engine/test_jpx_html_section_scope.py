"""Unrelated JPX sections must not supply a heading or an empty-result claim."""

import pytest

from baibai_engine.market.providers.jpx import JPXProvider, JPXProviderError


@pytest.mark.parametrize("has_target_heading", [False, True])
def test_unrelated_empty_marker_does_not_hide_missing_trading_halt_table(has_target_heading):
    html = "<h2>本日の売買停止情報</h2><p>layout changed</p>" if has_target_heading else ""
    html += "<h2>その他のお知らせ</h2><p>現在、該当する情報はありません。</p>"
    with pytest.raises(JPXProviderError, match="failed to locate JPX HTML table"):
        JPXProvider()._parse_trading_halt_html_rows(html, "https://www.jpx.co.jp/example")


def test_trading_halt_empty_result_belongs_to_target_section():
    html = (
        "<h2>本日の売買停止情報</h2><p>現在、該当する情報はありません。</p>"
        "<h2>その他のお知らせ</h2><p>別の情報</p>"
    )
    assert JPXProvider()._parse_trading_halt_html_rows(html, "https://www.jpx.co.jp/example") == []


def test_new_h2_does_not_inherit_prior_reorganization_heading():
    html = (
        "<h2>指定状況</h2><h3>整理銘柄</h3><p>layout changed</p>"
        "<h2>別の一覧</h2><table class='fixedhead'>"
        "<tr><th>指定日</th><th>会社</th><th>コード</th></tr>"
        "<tr><td>2026/10/15</td><td>別会社</td><td>1301</td></tr></table>"
    )
    with pytest.raises(JPXProviderError, match="failed to locate JPX HTML table"):
        JPXProvider()._parse_reorganization_html_rows(html, "https://www.jpx.co.jp/example")
