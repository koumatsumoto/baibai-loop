from copy import deepcopy

import pytest

from baibai_engine.investment_judgment.trade_proposal.report_perspectives import (
    MODELS,
    estimate_missing,
)

DAY = "2026-10-20"
CUT = DAY + "T15:00:00+09:00"
EARLIER = DAY + "T05:00:00Z"
LATE = DAY + "T06:00:01Z"
HASH = "a" * 64


def _sel(version=5):
    return {
        "schema_version": version,
        "targets": [{"ticker": "1234"}],
        "research_as_of": DAY,
        "input_known_at": CUT,
        "inputs": {
            "screening_published_at": EARLIER,
            "generation": {
                "as_of": DAY,
                "evaluation_time": EARLIER,
                "generation_id": "frozen",
                "subject_plan": {"1234": {"related_subjects": []}},
                "models": [],
            },
        },
    }


def _put(s, key, payload, subject="1234"):
    s["inputs"]["generation"]["models"].append(
        {"model_id": key, "subject": subject, "payload": payload}
    )


def _estimate(s, ticker="1234", recorded=frozenset()):
    result = estimate_missing(s, ticker=ticker, recorded=set(recorded), selection_sha256=HASH)
    return None if result is None else {r["model"]: r for r in result["rows"]}


def _external(*, when=EARLIER, ticker="1234", value=100.0):
    return {
        "ticker": ticker,
        "model_valuations": [
            {
                "inputs": {
                    "latest": {
                        "ticker": ticker,
                        "currency": "JPY",
                        "unit": "JPY/share",
                        "fetched_at_utc": when,
                        "snapshot_date": DAY,
                        "per_company_forecast": value,
                        "per_company_forecast_origin": "外部社モデル",
                        "source_url": "https://scouter.monex.co.jp/report/theoryDps/1234",
                    }
                }
            }
        ],
    }


def _technical(ticker="1234"):
    return {
        "ticker": ticker,
        "moving_averages": {
            "sma_20bar": {
                "status": "available",
                "window_end_date": "2026-10-19",
                "gap_ratio": -0.09,
            }
        },
        "price_momentum": {"status": "available", "end_date": "2026-09-17", "value": 0.115},
    }


def test_exact_scope_clock_and_author_priority_without_a_latest_fallback():
    s = _sel()
    _put(s, "external_assessment", _external())
    _put(s, "technical_analysis", _technical())
    rows = _estimate(s, recorded={"company_growth"})
    assert len(rows) == 6
    assert "company_growth" not in rows
    assert rows["external_assessment"]["assessment"] == "context_only"
    assert rows["external_assessment"]["source_url"].startswith("https://scouter.monex.co.jp/")
    assert "PER方式" in rows["external_assessment"]["summary"]
    assert rows["technical_analysis"]["assessment"] == "mixed"
    assert "2026-09-17" in rows["technical_analysis"]["summary"]
    assert _estimate(s, "0000") is None
    assert _estimate(s, recorded=set(MODELS)) is None
    for target, key in [
        (s["inputs"], "screening_published_at"),
        (s["inputs"]["generation"], "evaluation_time"),
    ]:
        previous = target[key]
        target[key] = LATE
        assert _estimate(s) is None
        target[key] = previous


@pytest.mark.parametrize(
    ("payload", "subject"),
    [
        (_external(when=LATE), "1234"),
        (_external(ticker="5678"), "1234"),
        (_external(), "5678"),
        (_external(value=True), "1234"),
        (_external(value=float("nan")), "1234"),
    ],
)
def test_other_subject_future_receipt_and_invalid_values_are_not_evidence(payload, subject):
    s = _sel()
    _put(s, "external_assessment", payload, subject)
    row = _estimate(s)["external_assessment"]
    assert row["assessment"] == "insufficient"
    assert row["observed_on"] is None


def test_v2_missing_screen_is_not_missing_independent_security_inputs_or_zero():
    s = _sel(2)
    s["inputs"] = {
        "analysis": {"analysis_as_of": None, "run_at": None},
        "securities_context": {
            "1234": {"technical_analysis": _technical(), "external_assessment": _external()}
        },
        "market_context": {"market": {"points": []}},
    }
    rows = _estimate(s)
    assert rows["company_financials"]["assessment"] == "insufficient"
    assert rows["technical_analysis"]["assessment"] == "mixed"
    assert rows["external_assessment"]["assessment"] == "context_only"
    s["inputs"]["securities_context"]["1234"]["technical_analysis"]["ticker"] = "5678"
    assert _estimate(s)["technical_analysis"]["assessment"] == "insufficient"
    del s["inputs"]["securities_context"]["1234"]["technical_analysis"]["ticker"]
    assert _estimate(s)["technical_analysis"]["assessment"] == "mixed"


def test_quarter_growth_is_difference_not_ratio_recovery_and_no_cherry_picking():
    s = _sel()
    p = {
        "ticker": "1234",
        "metric_units": {"sales": "JPY", "operating_profit": "JPY"},
        "periods": [{"period_end": "2026-09-30"}],
        "changes": [],
    }
    for metric, prior, current in (("sales", 200e6, 100e6), ("operating_profit", -20e6, 60e6)):
        p["changes"].append(
            {
                "measurement": "quarter",
                "metric": metric,
                "comparison": "year_over_year",
                "to_end": "2026-09-30",
                "from_end": "2025-09-30",
                "to_value": current,
                "from_value": prior,
                "difference": current - prior,
                "reason": "nonpositive_base" if prior < 0 else None,
            }
        )
    _put(s, "company_growth", p)
    r = _estimate(s)["company_growth"]
    assert r["assessment"] == "mixed"
    assert "-0.20→0.60億円" in r["summary"]
    p["periods"].append({"period_end": "2026-10-10"})
    assert _estimate(s)["company_growth"]["assessment"] == "insufficient"


def test_sector_origin_and_context_only_household_observation_no_investment_vote():
    s = _sel()
    p = {
        "master": {"sector_33": "合成業"},
        "relative": {
            "sector": {
                "sector_33": "合成業",
                "peer_count": 10,
                "peer_median_return_20d": 0,
                "comparison_start_date": "2026-09-17",
                "comparison_end_date": "2026-10-19",
            }
        },
    }
    _put(s, "market_context", p)
    m = {
        "series_id": "jp.cpi.core_yoy",
        "unit": "percent",
        "frequency": "monthly",
        "geography": "japan",
        "observed_at": "2026-08-01",
        "vintage_at": EARLIER,
        "latest_value": 3.0,
        "stale": False,
    }
    _put(
        s,
        "market_context",
        {
            "market": {
                "benchmark_ticker": "1321",
                "points": [
                    {
                        "observation_date": "2026-10-19",
                        "benchmark_return_20d": 0.0,
                        "benchmark_return_60d": 0.0,
                    }
                ],
            },
            "reading": {"series": [m]},
        },
        "market",
    )
    r = _estimate(s)
    assert r["market_context"]["assessment"] == "neutral"
    assert "1321の終値・20本前比 +0.0%" in r["market_context"]["summary"]
    assert "市場全体の方向" not in r["market_context"]["summary"]
    assert r["industry_trends"]["assessment"] == r["social_trends"]["assessment"] == "context_only"
    assert "中央値 +0.0%" in r["industry_trends"]["summary"]
    assert "ポイント" not in r["industry_trends"]["summary"]
    assert "2026年08月" in r["social_trends"]["summary"]
    p["master"]["sector_33"] = "他業種"
    m["vintage_at"] = LATE
    r = _estimate(s)
    assert r["industry_trends"]["assessment"] == r["social_trends"]["assessment"] == "insufficient"


@pytest.mark.parametrize(("excess", "shown"), [(0.0, "+0.0"), (-0.016, "-1.6")])
def test_sector_gap_uses_its_recorded_endpoints_not_the_market_calendar(excess, shown):
    s = _sel()
    # A thinly traded company's 20 observed price differences can span much longer.
    _put(
        s,
        "market_context",
        {
            "master": {"sector_33": "合成業"},
            "price_path_relative": {
                "comparisons": {"R20": {"comparison_start_date": "2026-09-17", "own_return": 0.5}}
            },
            "relative": {
                "sector": {
                    "sector_33": "合成業",
                    "peer_count": 3,
                    "peer_median_return_20d": -0.02,
                    "relative_return_20d": excess,
                    "comparison_start_date": "2026-08-17",
                    "comparison_end_date": "2026-10-19",
                }
            },
        },
    )
    row = _estimate(s)["industry_trends"]
    assert row["assessment"] == "context_only"
    assert "2026-08-17〜2026-10-19" in row["summary"]
    assert "20立会" not in row["summary"]
    assert "自社を除く3社" in row["summary"]
    assert "中央値 -2.0%" in row["summary"]
    assert f"（自社の騰落−同業中央値）は{shown}ポイント" in row["summary"]


def test_exact_legacy_financial_identity_only_and_duplicate_models_not_summed():
    s = _sel(3)
    fin = {
        "earnings_price": {
            "price_date": "2026-10-19",
            "profit_ttm_yen": -25e6,
            "earnings_yield": -0.05,
        }
    }
    _put(s, "company_valuation", fin)
    assert _estimate(s)["company_financials"]["assessment"] == "adverse"
    dup = deepcopy(s)
    _put(dup, "company_financials", fin)
    assert _estimate(dup)["company_financials"]["assessment"] == "insufficient"
    s["schema_version"] = 5
    assert _estimate(s)["company_financials"]["assessment"] == "insufficient"


@pytest.mark.parametrize(
    ("statement", "basis", "label", "allocation"),
    [
        ("consolidated", "reported_profit_before_common_share_allocations", "親会社帰属利益", True),
        ("nonconsolidated", "reported_profit_before_common_share_allocations", "単体純利益", True),
        ("consolidated", None, "親会社帰属利益", False),
        (None, None, "報告純利益", False),
    ],
)
def test_price_ratio_names_the_saved_profit_not_a_common_share_return(
    statement, basis, label, allocation
):
    s = _sel()
    ep = {"price_date": DAY, "profit_ttm_yen": 1e8, "earnings_yield": 0.05}
    if statement is not None:
        ep["statement_basis"] = statement
    f = {"earnings_price": ep}
    if basis is not None:
        f["earnings_basis"] = basis
    _put(s, "company_financials", f)
    row = _estimate(s)["company_financials"]
    assert row["assessment"] == "supportive"
    assert label in row["summary"]
    assert "時価総額比5.0%" in row["summary"]
    assert ("普通株への配分前" in row["summary"]) is allocation
    assert "EPS" not in row["summary"]


def test_cash_opposite_to_profit_only_when_end_and_accounting_basis_agree():
    s = _sel()
    _put(s, "market_context", {"master": {"sector_33": "機械", "snapshot_date": DAY}})
    f = {
        "earnings_price": {
            "price_date": "2026-10-19",
            "profit_ttm_yen": 20e6,
            "earnings_yield": 0.01,
            "profit_components": [{"period_end": "2026-09-30"}],
            "accounting_standard": "JP",
            "statement_basis": "consolidated",
        },
        "financial": {
            "ocf_ttm": -50e6,
            "ocf_ttm_period": {
                "end": "2026-09-30",
                "accounting_standard": "JP",
                "statement_basis": "consolidated",
            },
        },
    }
    _put(s, "company_financials", f)
    row = _estimate(s)["company_financials"]
    assert row["assessment"] == "mixed"
    assert "2026-09-30までの12か月" in row["summary"]
    assert "2026-10-19の時価総額比" in row["summary"]
    f["financial"]["ocf_ttm_period"]["end"] = "2026-03-31"
    assert _estimate(s)["company_financials"]["assessment"] == "supportive"
    assert "営業CF" not in _estimate(s)["company_financials"]["summary"]
    del f["earnings_price"]["profit_components"]
    assert "実績期末未記録" in _estimate(s)["company_financials"]["summary"]


def test_known_monetary_fact_not_its_future_plan_or_other_issuer_claim():
    s = _sel()

    def observation(day, status, rate):
        return {
            "claim": {
                "domain": "social",
                "kind": "reported_fact",
                "status": status,
                "occurred_on": day,
                "negated": False,
                "predicate": "overnight_call_rate_target",
                "attribution": "日本銀行",
                "scope": {
                    "scope_id": "jp-monetary-policy",
                    "target": "uncollateralized_overnight_call_rate",
                },
                "quantity": {"unit": "percent", "value": rate, "approximate": True},
            },
            "evidence": {"received_at": EARLIER, "observation_sha256": HASH},
            "exact_evidence": {
                "observation_sha256": HASH,
                "original_url": "https://www.boj.or.jp/",
            },
        }

    p = {
        "observations": [
            observation("2026-09-18", "decided", "1.25"),
            observation("2026-10-19", "scheduled", "9.99"),
        ]
    }
    _put(s, "social_trends", p, "market:global")
    r = _estimate(s)["social_trends"]
    assert r["assessment"] == "context_only"
    assert "1.25%" in r["summary"]
    assert "9.99" not in r["summary"]
    assert r["source_url"] == "https://www.boj.or.jp/"
    p["observations"][0]["evidence"]["received_at"] = LATE
    assert _estimate(s)["social_trends"]["assessment"] == "insufficient"


def test_industry_claim_itself_must_belong_to_the_fixed_subject_and_sample():
    s = _sel()
    g = s["inputs"]["generation"]
    g["subject_plan"]["1234"]["related_subjects"] = ["industry:example"]
    claim = {
        "domain": "industry",
        "kind": "reported_fact",
        "status": "reported",
        "negated": False,
        "scope": {
            "scope_id": "different",
            "period_end": "2026-03-31",
            "label": "合成業",
            "population_size": 21,
        },
        "attribution": "業種調査",
        "predicate": "profitable_company_share",
        "quantity": {"unit": "percent", "value": "75"},
    }
    p = {
        "observations": [
            {
                "claim": claim,
                "evidence": {"received_at": EARLIER, "observation_sha256": HASH},
                "exact_evidence": {"observation_sha256": HASH},
            }
        ]
    }
    _put(s, "industry_trends", p, "industry:example")
    assert _estimate(s)["industry_trends"]["assessment"] == "insufficient"
    claim["scope"]["scope_id"] = "example"
    r = _estimate(s)["industry_trends"]
    assert r["assessment"] == "context_only"
    assert "21社" in r["summary"]


def test_only_external_pbr_model_is_a_per_share_estimate_not_a_multiple():
    s = _sel()
    e = _external(value=None)
    obs = e["model_valuations"][0]["inputs"]["latest"]
    obs.update(pbr=18.0, pbr_origin="外部提供社", share_basis=None)
    _put(s, "external_assessment", e)
    r = _estimate(s)["external_assessment"]
    assert r["assessment"] == "context_only"
    assert "18円/株" in r["summary"]
    assert "PBR倍率ではなく" in r["summary"]
    assert "株数基準未確認" in r["summary"]
    obs["unit"] = "multiple"
    assert _estimate(s)["external_assessment"]["assessment"] == "insufficient"


@pytest.mark.parametrize(
    ("end", "disclosed", "clock", "accepted"),
    [
        ("2026-10-21", "2026-11-01", None, False),
        ("2026-09-30", "2026-11-01", None, False),
        ("2026-09-30", DAY, "15:00:01", False),
        ("2026-09-30", DAY, "14:00:01", False),
        ("2026-09-30", DAY, "14:00:00", True),
        ("2026-09-30", "2026-10-19", None, True),
    ],
)
def test_future_account_is_not_a_missing_vintage_or_a_known_profit(end, disclosed, clock, accepted):
    s = _sel()
    f = {
        "earnings_price": {
            "price_date": "2026-10-19",
            "earnings_yield": 0.05,
            "profit_ttm_yen": 900e6,
            "profit_components": [
                {"period_end": end, "disclosed_at": disclosed, "disclosed_time": clock}
            ],
        }
    }
    _put(s, "company_financials", f)
    assert _estimate(s)["company_financials"]["assessment"] == (
        "supportive" if accepted else "insufficient"
    )
    if not accepted:
        f["financial"] = {"latest_financial_disclosure_date": "2026-09-01", "equity_ratio": 0.45}
        row = _estimate(s)["company_financials"]
        assert row["assessment"] == "context_only"
        assert "45.0%" in row["summary"]
        assert "9.00億円" not in row["summary"]


def test_unfinished_report_uses_only_its_fixed_issuer_and_dated_master():
    from datetime import date, datetime

    from baibai_engine.investment_judgment.trade_proposal.report_perspectives import (
        saved_master_identity,
    )

    def identity(s, *, cut=CUT, ticker="1234"):
        return saved_master_identity(
            s,
            ticker=ticker,
            proposal_as_of=date.fromisoformat(DAY),
            prepared_at=datetime.fromisoformat(cut),
        )

    past = {"snapshot_date": "2026-10-19", "name": "親会社", "sector_33": "情報・通信業"}
    other = {**past, "name": "子会社"}
    s = _sel()
    _put(s, "market_context", {"master": other}, subject="4321")
    _put(s, "market_context", {"master": past})
    assert identity(s) == ("親会社", "情報・通信業")
    assert identity(s, ticker="4321") is None  # Not a selected target.
    assert identity(s, cut="2026-10-20T14:59:59.999999+09:00") is None
    s["inputs"]["generation"]["as_of"] = "2026-10-18"
    assert identity(s) is None
    s["inputs"]["generation"]["as_of"] = DAY
    s["inputs"]["generation"]["models"][-1]["payload"]["ticker"] = "4321"
    assert identity(s) is None
    del s["inputs"]["generation"]["models"][-1]["payload"]["ticker"]
    _put(s, "market_context", {"master": other})  # Ambiguous same subject.
    assert identity(s) is None

    legacy = {
        **s,
        "schema_version": 2,
        "inputs": {
            "securities_context": {
                "1234": {"market_context": {"master": {**past, "sector_33": None}}},
                "4321": {"market_context": {"master": other}},
            }
        },
    }
    assert identity(legacy) == ("親会社", None)
    legacy["inputs"]["securities_context"]["1234"]["market_context"]["master"] = {
        **past,
        "snapshot_date": "2026-10-21",
    }
    assert identity(legacy) is None


@pytest.mark.parametrize("sector", ["銀行業", "保険業", "その他金融業", None, "機械"])
@pytest.mark.parametrize("cf", [-1e8, 0.0, 1e8])
def test_cf_direction_is_not_a_financial_institution_owner_return(sector, cf):
    s = _sel()
    f = {"financial": {"edinet_ocf_ttm": cf, "edinet_source_period_end": "2026-03-31"}}
    _put(s, "company_financials", f)
    _put(s, "market_context", {"master": {"sector_33": sector, "snapshot_date": DAY}})
    original = deepcopy(s)
    row = _estimate(s)["company_financials"]
    assert row["assessment"] == (
        ("supportive" if cf > 0 else "adverse" if cf < 0 else "neutral")
        if sector == "機械"
        else "context_only"
    )
    assert f"{cf / 1e8:+,.2f}" in row["summary"]
    assert s == original
    assert "company_financials" not in _estimate(s, recorded={"company_financials"})
    if sector == "銀行業":
        assert "CET1" in row["summary"]
    f["earnings_price"] = {
        "price_date": DAY,
        "profit_ttm_yen": -100e6,
        "earnings_yield": -0.02,
        "profit_components": [{"period_end": "2026-09-30"}],
        "accounting_standard": "JP",
        "statement_basis": "consolidated",
    }
    f["financial"].update(
        {
            "ocf_ttm": cf,
            "ocf_ttm_period": {
                "end": "2026-09-30",
                "accounting_standard": "JP",
                "statement_basis": "consolidated",
            },
        }
    )
    joined = _estimate(s)["company_financials"]
    assert joined["assessment"] == ("mixed" if sector == "機械" and cf > 0 else "adverse")
    assert "営業CF" in joined["summary"]


@pytest.mark.parametrize("version", [2, 5])
@pytest.mark.parametrize(
    ("sector", "observed"), [("銀行業", "context_only"), ("機械", "supportive")]
)
@pytest.mark.parametrize(
    ("clocks", "known"),
    [
        ({}, True),  # Earlier snapshots may not have carried either clock.
        ({"edinet_source_submit_datetime": DAY + " 14:00"}, True),
        ({"edinet_source_submit_datetime": DAY + " 14:01"}, False),
        ({"edinet_source_submit_datetime": DAY + "T06:00:01Z"}, False),
        ({"edinet_source_submit_datetime": DAY}, False),
        ({"edinet_snapshot_recorded_at_utc": EARLIER}, True),
        ({"edinet_snapshot_recorded_at_utc": DAY + "T05:00:01Z"}, False),
        ({"edinet_snapshot_recorded_at_utc": DAY + " 04:00"}, False),
        (
            {
                "edinet_source_submit_datetime": DAY + " 14:00",
                "edinet_snapshot_recorded_at_utc": DAY + "T04:59:59Z",
            },
            False,
        ),
        ({"edinet_snapshot_asof": "2026-10-21"}, False),
    ],
)
def test_explicit_edinet_clocks_must_fit_the_financial_receipt(
    version, sector, observed, clocks, known
):
    s = _sel(version)
    financial = {"edinet_source_period_end": "2026-03-31", "edinet_ocf_ttm": 1e8, **clocks}
    market = {"master": {"sector_33": sector, "snapshot_date": DAY}}
    if version == 2:
        s["inputs"] = {
            "analysis": {
                "analysis_as_of": DAY,
                "run_at": EARLIER,
                "analyses": {"1234": {"metrics": financial}},
            },
            "securities_context": {"1234": {"market_context": market}},
        }
    else:
        _put(s, "company_financials", {"financial": financial})
        _put(s, "market_context", market)
    row = _estimate(s)["company_financials"]
    assert row["assessment"] == (observed if known else "insufficient")
    assert ("1.00億円" in row["summary"]) is known
    if not known:
        financial.update({"fin_latest_disclosed_date": DAY, "equity_ratio": 0.21})
        equity = _estimate(s)["company_financials"]
        assert equity["assessment"] == "context_only"
        assert "21.0%" in equity["summary"]


@pytest.mark.parametrize("sector", ["銀行業", "機械"])
def test_later_cash_component_cannot_qualify_earlier_reported_profit(sector):
    s = _sel()
    component = {"period_end": "2026-09-30", "disclosed_at": DAY, "disclosed_time": "14:00:00"}
    cash_component = {**component, "disclosed_time": "14:00:01"}  # generation < release < research
    basis = {"accounting_standard": "JP", "statement_basis": "consolidated"}
    _put(s, "market_context", {"master": {"sector_33": sector, "snapshot_date": DAY}})
    _put(
        s,
        "company_financials",
        {
            "earnings_price": {
                "price_date": DAY,
                "profit_ttm_yen": 100e6,
                "earnings_yield": 0.03,
                "profit_components": [component],
                **basis,
            },
            "financial": {
                "ocf_ttm": -100e6,
                "ocf_ttm_period": {"end": "2026-09-30", "components": [cash_component], **basis},
            },
        },
    )
    row = _estimate(s)["company_financials"]
    assert row["assessment"] == "supportive"
    assert "-1.00億円" not in row["summary"]
    assert "ocf_ttm" not in row["evidence"]
