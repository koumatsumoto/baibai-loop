import pytest

from baibai_engine.machine_analysis.observations.disclosures.jpx_decode import (
    build_regulation_snapshot,
    parse_current_listing_designations,
    parse_regulation_rows,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_models import JPXProviderError
from baibai_engine.machine_analysis.observations.disclosures.jpx_sources import JPX_REGULATION_URLS

URL = JPX_REGULATION_URLS["整理銘柄"]
CHECK, REVIEW, DELIST = "監理銘柄（確認中）", "監理銘柄（審査中）", "整理銘柄"


def section(label, *codes):
    rows = (
        "<table class='fixedhead'><tr><th>指定年月日</th><th>銘柄名</th><th>コード</th></tr>"
        + "".join(f"<tr><td>2026/09/17</td><td>対象</td><td>{code}</td></tr>" for code in codes)
        + "</table>"
        if codes
        else "<p>現在、該当する情報はありません。</p>"
    )
    return f"<h3><span>{label}</span></h3>{rows}"


def current(*sections):
    return "<h2>現在の指定状況</h2>" + "".join(sections)


def test_current_designations_and_daily_reorganization_use_the_same_page_sections():
    html = current(section(CHECK, "130A"), section(REVIEW, "2200"), section(DELIST, "3300"))
    html += "<h2>指定・解除履歴</h2>" + section(CHECK, "9991") + section(DELIST, "9992")
    assert parse_current_listing_designations(html.encode(), URL) == {
        "130A": (CHECK,),
        "2200": (REVIEW,),
        "3300": (DELIST,),
    }
    # The daily four-source projection includes reorganization, not the two supervision types.
    daily_rows = parse_regulation_rows(DELIST, html.encode(), URL)
    assert build_regulation_snapshot({DELIST: daily_rows}).flags_by_ticker == {"3300": (DELIST,)}


@pytest.mark.parametrize("history", ["", "<h2>指定・解除履歴</h2>" + section(DELIST, "8181")])
def test_daily_reorganization_treats_explicit_current_empty_as_zero_even_with_history(history):
    html = current(section(CHECK, "130A"), section(REVIEW), section(DELIST)) + history
    assert parse_regulation_rows(DELIST, html.encode(), URL) == []
    assert parse_current_listing_designations(html.encode(), URL) == {"130A": (CHECK,)}


def test_daily_reorganization_cannot_fill_a_missing_current_subsection_from_history():
    html = (
        current(section(CHECK), section(REVIEW))
        + "<h2>指定・解除履歴</h2>"
        + section(DELIST, "8181")
    )
    with pytest.raises(JPXProviderError):
        parse_regulation_rows(DELIST, html.encode(), URL)


def test_explicitly_empty_current_subsection_is_not_a_missing_table_or_a_cleared_other_status():
    html = current(section(CHECK, "130A"), section(REVIEW), section(DELIST, "3300"))
    assert parse_current_listing_designations(html.encode(), URL) == {
        "130A": (CHECK,),
        "3300": (DELIST,),
    }


@pytest.mark.parametrize(
    "html",
    [
        current(section(CHECK, "130A"), section(DELIST, "3300")),
        current(section(CHECK, "130A"), section(DELIST, "3300"))
        + "<h2>履歴</h2>"
        + section(REVIEW),
        current(section(CHECK, "130A"), f"<h3>{REVIEW}</h3>表示変更", section(DELIST)),
        current(section(CHECK, "130A"), section(REVIEW), section(DELIST, "3300")).replace(
            "class='fixedhead'", "class='unknown'", 1
        ),
        current(section(CHECK, "130A"), section(CHECK, "331A"), section(REVIEW), section(DELIST)),
        current(section(CHECK, "130A"), section(REVIEW), section(DELIST, "33001")),
    ],
)
def test_incomplete_ambiguous_or_invalid_current_page_is_unavailable_not_an_empty_result(html):
    with pytest.raises(JPXProviderError):
        parse_current_listing_designations(html.encode(), URL)


def test_other_source_cannot_supply_a_listing_designation():
    html = current(section(CHECK), section(REVIEW), section(DELIST))
    with pytest.raises(JPXProviderError, match="source"):
        parse_current_listing_designations(html.encode(), JPX_REGULATION_URLS["取引停止"])
