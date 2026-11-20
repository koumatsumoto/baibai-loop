"""日本基準・明示的に遡及した中間期の表紙。値は合成で原本の転載ではない。"""

from datetime import UTC, date, datetime

import pytest
from tests.engine.machine_analysis.observations.financials.test_pdf_jp_restatement import (
    later_interim_docs,
    pdf,
)

from baibai_engine.machine_analysis.generation.financial_models import (
    generate_financial_and_price_models,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.models.company_growth.reading import (
    CompanyGrowthReader,
    metric_value,
)
from baibai_engine.machine_analysis.observations.financials.pdf_statements import (
    PdfStatement,
    extract_pages,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar

H1 = """2027年3月期 第2四半期（中間期）決算短信〔日本基準〕(連結)
2026年11月4日
上場会社名 テスト コード番号 9127
(百万円未満切捨て)
1．2027年3月期第2四半期（中間期）の連結業績（2026年4月1日～2026年9月30日）
(1)連結経営成績(累計) (％表示は、対前年中間期増減率)
売上高 営業利益 経常利益 親会社株主に帰属する中間純利益
百万円 ％ 百万円 ％ 百万円 ％ 百万円 ％
2027年3月期中間期 2,869 14.0 654 ― 660 ― 440 ―
2026年3月期中間期 2,473 ― 125 ― 49 ― 71 ―
(注) 包括利益 2027年3月期中間期 511百万円(190.2％) 2026年3月期中間期 165百万円(―％)
(注)2026年3月期中間期の数値は、会計方針の変更に伴い遡及修正が行われたため、遡及後の数値に変更しております。
(2)連結財政状態
総資産 純資産"""


def interim(text=H1):
    return extract_pages(
        PdfStatement(
            ticker="9127",
            disclosure_number="later-interim",
            source_url="https://example.invalid/synthetic",
            source_sha256="c" * 64,
            bundle_id="synthetic",
            resource_name="pdf:later-interim",
            received_at=datetime(2026, 11, 5, tzinfo=UTC),
        ),
        [text],
    )


@pytest.mark.parametrize("same_day_reference", [False, True])
def test_interim_cumulative_display_does_not_borrow_annual_reference(same_day_reference):
    item = interim()
    assert item.reason is None
    assert (item.current_start, item.current_end, item.comparative_start, item.comparative_end) == (
        date(2026, 4, 1),
        date(2026, 9, 30),
        date(2025, 4, 1),
        date(2025, 9, 30),
    )
    assert [(x.metric, x.current, x.comparative) for x in item.pairs] == [
        ("sales", 2869, 2473),
        ("operating_profit", 654, 125),
        ("parent_profit", 440, 71),
    ]
    assert item.pairs[-1].row == "親会社株主に帰属する中間純利益"
    annual = pdf(True)
    if same_day_reference:
        # Same issuer, date, annual fiscal end, unit and amounts are insufficient.
        # This source restates an annual operand for another accounting-policy change.
        annual = annual.model_copy(
            update={
                "published_on": item.published_on,
                "source_sha256": "d" * 64,
                "annual_reference": annual.annual_reference.model_copy(
                    update={"basis_text": "別の会計方針の変更による前期連結実績"}
                ),
            }
        )
    financial, growth, *_ = generate_financial_and_price_models(
        ticker="9127",
        documents=later_interim_docs(),
        adjusted_bars=[PriceBar(date(2026, 11, 13), 3295, None)],
        adjustment_events=[],
        trading_dates=[date(2026, 11, 13)],
        expected_price_date=date(2026, 11, 13),
        sector="海運業",
        as_of=date(2026, 11, 14),
        input_release=InputRelease(release_id="synthetic", manifest_sha256="a" * 64),
        ttm_rules=DEFAULT_TTM_RULES,
        pdf_statements=[pdf(), annual, item],
    )
    sel = financial.payload["financial"]["pdf_selection"]
    assert sel == growth.payload["pdf_selection"]
    assert sel["reason"] == "jp_interim_annual_restatement_basis_unverified"
    assert sel["blocks_legacy_ttm"]
    assert sel["annual"] is None
    assert not sel["amounts_yen"]
    assert sel["current"]["disclosure_number"] == "later-interim"
    assert financial.payload["earnings_price"]["profit_ttm_yen"] is None
    assert financial.payload["earnings_price"]["earnings_yield"] is None
    assert financial.payload["earnings_price"]["market_cap_yen"] == 5_041_251_150
    changes = [
        c
        for c in growth.payload["changes"]
        if c["metric"] == "parent_profit"
        and c["measurement"] == "reported"
        and c["comparison"] == "year_over_year"
        and c["to_end"] == "2026-09-30"
    ]
    assert [(c["from_value"], c["to_value"], c["source_sha256"]) for c in changes] == [
        (71_000_000, 440_000_000, item.source_sha256)
    ]
    reader = CompanyGrowthReader(growth.payload)
    assert (
        metric_value(reader.observation(reader.latest_period, "reported"), "parent_profit")
        == 440_000_000
    )
    assert (
        metric_value(reader.observation(reader.latest_period, "annual_ttm"), "parent_profit")
        is None
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("第2四半期（中間期）決算短信", "第1四半期決算短信"),
        ("2026年3月期中間期 2,473", "2026年3月期第1四半期 2,473"),
        ("2027年3月期中間期 2,869", "2028年3月期中間期 2,869"),
        ("2026年9月30日", "2026年6月30日"),
        ("中間期の数値は", "第1四半期の数値は"),
        ("(注)2026年3月期中間期", "(2)連結財政状態\n(注)2026年3月期中間期"),
        ("百万円 ％ 百万円 ％ 百万円 ％ 百万円 ％", "千円 ％ 千円 ％ 千円 ％ 千円 ％"),
        ("連結経営成績(累計)", "連結業績予想(累計)"),
        ("親会社株主に帰属する中間純利益", "当期純利益"),
        ("〔日本基準〕(連結)", "〔日本基準〕(非連結)"),
        (
            "2027年3月期中間期 2,869",
            "正しい単位/見出しは前の表だけ\n個別利益予想\n2027年3月期中間期 2,869",
        ),
        ("2026年3月期中間期 2,473", "別の表\n2026年3月期中間期 2,473"),
    ],
)
def test_interim_rows_unit_period_and_note_cannot_borrow_from_elsewhere(old, new):
    item = interim(H1.replace(old, new))
    assert item.reason
    assert not item.pairs


def test_interim_dates_from_another_heading_do_not_authorize_actual_table():
    text = H1.replace("2026年9月30日", "2026年6月30日").replace(
        "(百万円未満切捨て)", "別表の連結業績（2026年4月1日～2026年9月30日）\n(百万円未満切捨て)"
    )
    assert interim(text).reason == "pdf_headline_period_conflict"


def test_interim_note_can_follow_the_same_periods_eps_table():
    source = H1.replace(
        "(注)2026年3月期中間期",
        "1株当たり\n中間純利益\n潜在株式調整後\n1株当たり中間純利益\n円 銭 円 銭\n"
        "2027年3月期中間期 275.24 ―\n2026年3月期中間期 42.38 ―\n(注)2026年3月期中間期",
    )
    assert interim(source).reason is None
    assert interim(source.replace("2026年3月期中間期 42.38", "2026年3月期第1四半期 42.38")).reason


@pytest.mark.parametrize("location", ["preamble", "forecast", "duplicate", "unmarked", "negated"])
def test_interim_footnote_is_not_a_page_wide_substring(location):
    note = next(line for line in H1.splitlines() if "遡及修正" in line)
    if location == "preamble":
        text = H1.replace(note, "").replace(
            "(百万円未満切捨て)", "別紙参考：" + note.removeprefix("(注)") + "\n(百万円未満切捨て)"
        )
    elif location == "forecast":
        text = H1.replace(note, "別表・業績予想\n" + note)
    elif location == "duplicate":
        text = H1.replace(note, note + "\n" + note)
    elif location == "unmarked":
        text = H1.replace(note, note.removeprefix("(注)"))
    else:
        text = H1.replace("変更しております。", "変更しておりません。")
    assert interim(text).reason == "pdf_comparative_basis_not_explicit"


@pytest.mark.parametrize("token", ["9127A", "91270", "9127  コード番号 9999"])
def test_interim_requires_one_complete_issuer_code(token):
    assert (
        interim(H1.replace("コード番号 9127", "コード番号 " + token)).reason
        == "pdf_issuer_mismatch"
    )


@pytest.mark.parametrize("other_table", [False, True])
def test_interim_rounding_must_immediately_label_the_actual_headline(other_table):
    text = H1.replace("(百万円未満切捨て)", "")
    if other_table:
        text = text.replace("総資産 純資産", "総資産 純資産\n(百万円未満切捨て)")
    else:
        text = text.replace("1．", "別表の丸め条件は(百万円未満切捨て)\n1．", 1)
    item = interim(text)
    assert item.reason == "pdf_precision_unknown"
    assert not item.pairs
