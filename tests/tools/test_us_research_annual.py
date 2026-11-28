"""Original SEC annual columns only; these are synthetic, fixed-receipt reports."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from tests.tools.test_us_research_mvp import _public_index_bundle, _replace_body
from tools.experiments.us_research_mvp import _compare_sec_report, _fixture, main


def _annual_receipts(current_days=364, prior_days=364):
    bundle, files, index = _public_index_bundle()
    subject = bundle["subjects"][0]
    subject["filing"]["form"] = "10-K"
    end = date.fromisoformat(subject["period_end"])
    current = end - timedelta(days=current_days - 1)
    prior_end = current - timedelta(days=1)
    prior = prior_end - timedelta(days=prior_days - 1)
    subject["period_start"] = current.isoformat()
    record = subject["companyfacts"]
    facts = json.loads(files[record["file"]])
    gaap = facts["facts"]["us-gaap"]
    entry = next(iter(gaap.values()))
    (row,) = entry["units"]["USD"]
    sample = {**row, "form": "10-K"}
    entry["units"]["USD"] = [
        dict(sample, start=current.isoformat(), end=end.isoformat(), val=1250),
        dict(sample, start=prior.isoformat(), end=prior_end.isoformat(), val=1000),
        dict(sample, start="2023-10-01", end="2024-09-30", val=9999),
        dict(
            sample,
            start=prior.isoformat(),
            end=prior_end.isoformat(),
            val=8888,
            accn="0000320193-25-000099",
        ),
    ]
    gaap["OperatingIncomeLoss"] = {
        "units": {
            "USD": [
                dict(sample, start=current.isoformat(), end=end.isoformat(), val=150),
                dict(sample, start=prior.isoformat(), end=prior_end.isoformat(), val=100),
            ]
        }
    }
    gaap["NetCashProvidedByUsedInOperatingActivities"] = {
        "units": {
            "USD": [
                dict(sample, start=current.isoformat(), end=end.isoformat(), val=195),
                # The report shows the prior USD cell is absent, not zero or copied from EUR.
            ],
            "EUR": [dict(sample, start=prior.isoformat(), end=prior_end.isoformat(), val=130)],
        }
    }
    gaap["PaymentsToAcquirePropertyPlantAndEquipment"] = {
        "units": {
            "USD": [
                dict(sample, start=current.isoformat(), end=end.isoformat(), val=55),
                dict(sample, start=prior.isoformat(), end=prior_end.isoformat(), val=50),
            ]
        }
    }
    _replace_body(record, files, facts)
    bundle.pop("alpaca")
    return bundle, files, index, [subject["cik"], "FY", prior.isoformat(), prior_end.isoformat()]


@pytest.mark.parametrize(
    ("current_days", "prior_days"), [(364, 371), (371, 364), (365, 366), (366, 365)]
)
def test_annual_sec_cli_compares_same_filing_full_years_and_never_produces_q4(
    tmp_path,
    capsys,
    current_days,
    prior_days,
):
    bundle, files, index, args = _annual_receipts(current_days, prior_days)
    subject = bundle["subjects"][0]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(bundle), encoding="utf-8")
    for name in (subject["companyfacts"]["file"], index["file"]):
        (tmp_path / name).write_bytes(files[name])
    assert main(["--bundle", str(manifest), "--compare-sec", *args]) == 0
    report = capsys.readouterr().out
    assert "指定会計年度 FY" in report
    assert "日数差は調整しない" in report
    assert (
        f"revenue (annual, current) {subject['period_start']}〜{subject['period_end']}: USD 1,250"
        in report
    )
    assert f"revenue (annual, prior) {args[2]}〜{args[3]}: USD 1,000" in report
    assert "revenue (annual): 差額 USD +250; 前年比 +25.00%" in report
    assert "operating_income (annual): 差額 USD +50; 前年比 +50.00%" in report
    assert "operating_cash_flow (annual, current)" in report
    assert "operating_cash_flow (annual): 未算出: `prior:no_fact_for_scope`" in report
    assert "ppe_cash_paid (annual, current)" in report
    for excluded in (
        "ppe_cash_paid (annual):",
        "(quarter",
        "year_to_date",
        "9,999",
        "8,888",
        "SMA",
        "research_candidate",
        "TTM",
        "FCF",
    ):
        assert excluded not in report


@pytest.mark.parametrize("both_sources", [False, True])
def test_fy_sources_show_which_original_pins_the_current_year(both_sources):
    bundle, files, index, args = _annual_receipts()
    subject = bundle["subjects"][0]
    filing = subject["filing"]
    if both_sources:
        witness = _fixture()[0]["subjects"][0]["filing"]
        filing.update(
            published_at=witness["published_at"], publication_ref=witness["publication_ref"]
        )
    output = _compare_sec_report(bundle, files.__getitem__, args)
    assert f"当期末照合: {subject['period_end']}" in output
    assert output.count(index["request_url"]) == 1
    assert index["received_at"].removesuffix("Z") in output
    assert "`sec-index:" in output
    if both_sources:
        assert f"独立公表証拠 `{witness['publication_ref']}`" in output


@pytest.mark.parametrize(
    ("defect", "reason"),
    [
        ("wrong_current_end", "sec_filing_period_mismatch"),
        ("no_index", "sec_filing_period_unverified"),
        ("late_index", "sec_filing_period_unverified"),
        ("prior_gap", "sec_invalid_fixed_evidence"),
        ("prior_overlap", "sec_invalid_fixed_evidence"),
        ("short_prior", "sec_invalid_fixed_evidence"),
        ("transition", "sec_period_outside_supported_fiscal_year"),
        ("quarter", "sec_filing_form_not_covered"),
        ("quarter_amendment", "sec_filing_form_not_covered"),
        ("amendment", "sec_filing_form_not_covered"),
        ("earnings_release", "sec_filing_form_not_covered"),
        ("transition_form", "sec_filing_form_not_covered"),
    ],
)
def test_fy_comparison_does_not_relabel_prior_or_ineligible_periods(defect, reason):
    bundle, files, index, args = _annual_receipts()
    subject = bundle["subjects"][0]
    filing = subject["filing"]
    if defect == "wrong_current_end":
        subject["period_start"], subject["period_end"] = args[-2:]
        prior_end = date.fromisoformat(subject["period_start"]) - timedelta(days=1)
        args[-2:] = [(prior_end - timedelta(days=363)).isoformat(), prior_end.isoformat()]
    elif defect in {"no_index", "late_index"}:
        witness = _fixture()[0]["subjects"][0]["filing"]
        filing["published_at"], filing["publication_ref"] = (
            witness["published_at"],
            witness["publication_ref"],
        )
        if defect == "no_index":
            del filing["public_index"]
        else:
            index["received_at"] = "2099-01-01T00:00:00Z"
    elif defect in {"prior_gap", "prior_overlap", "short_prior"}:
        delta = timedelta(days=-1 if defect == "prior_gap" else 1)
        args[2] = (date.fromisoformat(args[2]) + delta).isoformat()
        if defect != "short_prior":
            args[3] = (date.fromisoformat(args[3]) + delta).isoformat()
    elif defect == "transition":
        subject["period_start"] = (
            date.fromisoformat(subject["period_start"]) + timedelta(days=15)
        ).isoformat()
    else:
        filing["form"] = {
            "quarter": "10-Q",
            "quarter_amendment": "10-Q/A",
            "amendment": "10-K/A",
            "earnings_release": "8-K",
            "transition_form": "10-KT",
        }[defect]
    report = _compare_sec_report(bundle, files.__getitem__, args)
    assert f"未採用: `{reason}`" in report
    assert "revenue (annual" not in report
