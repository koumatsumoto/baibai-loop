"""A selected native receipt may add fiscal context; never overwrite a model price."""

import hashlib
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from tests.helpers.external_assessment_html import monex_html
from tests.helpers.external_assessment_responses import response

from baibai_engine.cli.external_assessment import main
from baibai_engine.data_collection.receipts.public_pages.receipts import insert_response
from baibai_engine.machine_analysis.observations.external_assessments.monex_receipt import (
    read_monex_eps,
)
from baibai_engine.machine_analysis.observations.external_assessments.normalization import (
    document,
    monex,
)
from baibai_engine.machine_analysis.observations.external_assessments.normalization.decode import (
    decode_attempt,
)
from baibai_engine.machine_analysis.observations.external_assessments.normalization.monex_eps import (
    reference_eps,
)
from baibai_engine.machine_analysis.observations.external_assessments.store import save_observations
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

NOW = datetime(2025, 5, 5, 1, 0, 0, 123456, tzinfo=UTC)


def row(label, period, value):
    period_html = f'<span class="thin_str">{period}</span>' if period else ""
    return (
        f'<tr><th class="set_word">{label}{period_html}</th>'
        f'<td class="td_left_data">{value}</td><td class="td_right_data">99.5％</td></tr>'
    )


def section(rows=None):
    if rows is None:
        rows = [
            row("前期実績", "2024/09", "88.0円"),
            row("今期会社予想", "2025/09", "99.0円"),
            row("今期予想コンセンサス", "2025/09", "-3.5円"),
            row("1年後コンセンサス", None, "123.4円"),
            row("来期予想コンセンサス", "2026/09", "0.0円"),
        ]
    return (
        '<div class="reference_table_left"><div class="table_title">参考EPS</div>'
        '<table class="table_left"><tr><th></th><td>EPS</td><td>前期比</td></tr>'
        + "".join(rows)
        + "</table>"
        '<div class="calc_area_comments">※EPSは最終更新日付時点の株数にて計算した値を表示</div>'
        '<div class="calc_area_comments">※EPSは12か月換算した値を表示</div></div>'
        "<p>ページ更新 2025-05-04 20:00:00、目標株価の人数：35人</p>"
    )


def test_individual_fiscal_rows_and_rolling_horizon_do_not_borrow_other_context():
    eps = reference_eps(document(section()))
    assert eps.annualized_to_12_months is True
    assert eps.share_basis == "shares_at_provider_last_update"
    assert (eps.current_fy.fiscal_end_label, eps.current_fy.eps_jpy_per_share) == ("2025/09", -3.5)
    assert (eps.next_fy.fiscal_end_label, eps.next_fy.eps_jpy_per_share) == ("2026/09", 0)
    assert eps.rolling_one_year.fiscal_end_label is None
    assert eps.rolling_one_year.eps_jpy_per_share == 123.4
    assert [
        (v.analyst_count, v.provider_published_at_utc)
        for v in (eps.current_fy, eps.next_fy, eps.rolling_one_year)
    ] == [(None, None)] * 3


def test_missing_or_invalid_row_is_not_zero_company_or_rolling_eps():
    rows = [
        row("今期予想コンセンサス", "2025/09", "－"),
        row("今期会社予想", "2025/09", "713円"),
        row("1年後コンセンサス", None, "512円"),
        row("来期予想コンセンサス", "2026/13", "314円"),
    ]
    html = section(rows).replace("※EPSは", "別注記")
    eps = reference_eps(document(html))
    assert eps.current_fy.fiscal_end_label == "2025/09"
    assert eps.current_fy.eps_jpy_per_share is None
    assert eps.next_fy is None
    assert eps.annualized_to_12_months is None
    assert eps.share_basis is None
    assert eps.rolling_one_year.eps_jpy_per_share == 512


@pytest.mark.parametrize(
    "extra",
    [
        "",
        section() + section(),
        section().replace("<td>EPS</td><td>前期比</td>", "<td>前期比</td><td>EPS</td>"),
        section().replace("参考EPS", "参考PER"),
    ],
)
def test_optional_absent_or_ambiguous_table_leaves_all_four_original_prices_unchanged(extra):
    kwargs = {"ticker": "7203", "source_url": monex.public_url("7203"), "fetched_at_utc": NOW}
    assert reference_eps(document(monex_html() + extra)) is None
    assert monex.parse_html(monex_html() + extra, **kwargs) == monex.parse_html(
        monex_html(), **kwargs
    )


@pytest.mark.parametrize(
    "rows",
    [
        [
            row("今期予想コンセンサス", "2026/03", "77.7円"),
            row("来期予想コンセンサス", "2026/03", "0円"),
        ],
        [
            row("今期予想コンセンサス", "2026/03", "58.2％"),
            row("来期予想コンセンサス", None, "77円"),
        ],
        [
            row("今期予想コンセンサス", "2026/03", "4円"),
            row("今期予想コンセンサス", "2026/03", "8円"),
            row("来期予想コンセンサス", "2027/03", '<img class="summary_blur">'),
        ],
    ],
)
def test_duplicate_or_wrong_period_units_and_locked_cell_are_not_forecasts(rows):
    eps = reference_eps(document(section(rows)))
    assert (eps.current_fy, eps.next_fy, eps.rolling_one_year) == (None, None, None)


def test_explicit_receipt_adds_only_opt_in_context_and_never_changes_the_store(tmp_path, capsys):
    db = tmp_path / "market.sqlite"
    body = (monex_html() + section()).encode()
    native = response("monex", "7203", body, url=monex.public_url("7203"), received_at_utc=NOW)
    with closing(open_connection(db)) as conn, conn:
        insert_response(conn, native)
    old = decode_attempt([native], expected_parser_version="monex-theory-4")
    save_observations(db, [old])
    before = hashlib.sha256(db.read_bytes()).digest()
    args = ["show", "--db", str(db), "--ticker", "7203", "--cutoff", NOW.isoformat()]
    assert main(args) == 0
    baseline = json.loads(capsys.readouterr().out)
    assert "selected_monex_eps_receipt" not in baseline
    assert main([*args, "--monex-eps-receipt-id", native.receipt_id]) == 0
    augmented = json.loads(capsys.readouterr().out)
    selected = augmented.pop("selected_monex_eps_receipt")
    assert augmented == baseline
    assert selected["receipt_id"] == native.receipt_id
    assert selected["attempt_id"] == native.attempt_id
    assert selected["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert datetime.fromisoformat(selected["received_at_utc"]) == NOW
    assert selected["reference_eps"]["current_fy"]["provider_label"] == "今期予想コンセンサス"
    assert selected["reference_eps"]["next_fy"]["eps_jpy_per_share"] == 0
    assert (
        old.model_dump_json()
        == decode_attempt([native], expected_parser_version="monex-theory-4").model_dump_json()
    )
    for ticker, cutoff in [("7203", NOW - timedelta(microseconds=1)), ("130A", NOW)]:
        with pytest.raises(ValueError, match="no matching retained"):
            read_monex_eps(db, receipt_id=native.receipt_id, ticker=ticker, cutoff=cutoff)
    assert hashlib.sha256(db.read_bytes()).digest() == before
