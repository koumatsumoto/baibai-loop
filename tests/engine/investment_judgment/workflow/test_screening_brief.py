"""briefは保存政策を変えず、既知の観測と比較不能を圧縮表示する。"""

import json
from copy import deepcopy

from baibai_engine.investment_judgment.workflow.screening_brief import economic_screening_brief


def card():
    return {
        "ticker": "1234",
        "name": "合成",
        "sector": "food",
        "rank": 1,
        "priority": 0.5,
        "strongest_path": "business_expansion",
        "model_revisions": {"company_growth": "v1", "sixth_probe": "v2"},
        "not_generated": ["seventh_probe"],
        "paths": [
            {
                "path_id": "business_expansion",
                "variant": "annual_ttm",
                "score": 0.5,
                "price_value": 0.2,
                "business_value": 0.1,
                "price_percentile": 0.5,
                "business_percentile": 0.5,
                "price_population": 1,
                "business_population": 1,
                "evidence": {
                    "book_provenance": "unverified_book",
                    "business_observation": {
                        "measurement": "annual_ttm",
                        "period_start": "2025-07-01",
                        "period_end": "2026-06-30",
                        "statement_basis": "consolidated",
                        "observation": {"documents": ["source"]},
                    },
                },
            }
        ],
        "context": {
            "related_subjects": ["sector:food"],
            "financial": {
                "cash": 10.0,
                "debt": None,
                "edinet_ocf_ttm": -5.0,
                "capex_ttm": 7.0,
                "fcf_ttm": -12.0,
                "edinet_failure_reasons": "debt_unavailable",
                "edinet_source_period_end": "2026-03-31",
            },
            "external_assessment": {"comparisons": []},
        },
    }


def result(company):
    return {"policy": {"method": "best-economic-path-v1"}, "eligible_count": 1, "cards": [company]}


def test_external_keeps_current_levels_states_periods_and_all_genuine_changes():
    company = card()
    rows = []
    for state, value, comparable in (
        ("same_value", 1.0, True),
        ("first_observation", -2.0, False),
        ("recovered", 3.0, False),
        ("count_changed", 4.0, False),
        ("not_comparable", -5.0, False),
        ("missing", None, False),
    ):
        rows.append(
            {
                "kind": "quants_scores",
                "distributor": "same-provider",
                "origin": "origin-a",
                "metric": state,
                "state": state,
                "latest_value": value,
                "comparable": comparable,
                "reason": "period_changed" if state == "not_comparable" else None,
                "previous_value": 7.0 if state == "not_comparable" else value,
                "target_period": "FY2027",
                "basis": {"unit": "points"},
            }
        )
    for index in range(6):
        rows.append(
            {
                "kind": "quants_scores",
                "distributor": "same-provider",
                "origin": "origin-b",
                "metric": f"changed_{index}",
                "state": "increased" if index % 2 else "decreased",
                "latest_value": 1.0,
                "previous_value": 2.0,
                "delta": -1.0,
                "comparable": True,
                "target_period": "FY2028",
                "previous_target_period": "FY2028",
                "basis": {"unit": "points"},
            }
        )
    company["context"]["external_assessment"]["comparisons"] = rows
    before = deepcopy(company)
    brief = economic_screening_brief(result(company))
    external = brief["cards"][0]["context"]["external_assessment"]
    assert sum(external["state_counts"].values()) == len(rows)
    assert len(external["comparable_changes"]) == 6  # 任意の先頭4件へ切らない。
    assert {row["state"] for row in external["comparable_changes"]} == {"increased", "decreased"}
    assert len(external["sources"]) == 2  # providerが同じでもorigin/対象期を誤結合しない。
    first = external["sources"][0]
    assert first["origin"] == "origin-a"
    assert first["target_period"] == "FY2027"
    states = {group["state"]: group for group in first["states"]}
    assert states["same_value"]["metrics"]["same_value"] == 1.0
    assert states["first_observation"]["metrics"]["first_observation"] == -2.0
    assert states["not_comparable"]["previous_values"]["not_comparable"] == 7.0
    assert states["not_comparable"]["reason"] == "period_changed"
    assert states["missing"]["metrics"]["missing"] is None
    assert "not_evidence_of_safety" in brief["presentation"]["missing"]
    assert company == before


def test_growth_retains_bad_latest_quarter_and_only_distinct_selected_fallback():
    company = card()
    quarter = {
        "measurement": "quarter",
        "from_start": "2025-04-01",
        "from_end": "2025-06-30",
        "to_start": "2026-04-01",
        "to_end": "2026-06-30",
        "from_value": 10.0,
        "to_value": -5.0,
        "difference": -15.0,
        "rate": -1.5,
        "reason": None,
    }
    annual = {
        **quarter,
        "measurement": "annual_ttm",
        "from_value": 20.0,
        "to_value": 30.0,
        "difference": 10.0,
        "rate": 0.5,
    }
    company["context"]["latest_growth_changes"] = {
        "operating_profit": {
            "latest_quarter": quarter,
            "selected_variant": "annual_ttm",
            "selected_comparison": annual,
        },
        "sales": {
            "latest_quarter": quarter,
            "selected_variant": "quarter",
            "selected_comparison": quarter,
        },
    }
    brief = economic_screening_brief(result(company))
    context = brief["cards"][0]["context"]
    changes = context["latest_growth_changes"]
    assert len(changes["latest_quarter"]) == 1
    assert changes["latest_quarter"][0]["metrics"]["operating_profit"]["to_value"] == -5.0
    assert changes["latest_quarter"][0]["to_end"] == "2026-06-30"
    assert changes["selected_fallbacks"][0]["metrics"]["operating_profit"]["to_value"] == 30.0
    assert "sales" not in changes["selected_fallbacks"][0]["metrics"]
    assert context["financial"]["debt"] is None
    assert context["financial"]["fcf_ttm"] == -12.0
    assert brief["common_model_revisions"]["sixth_probe"] == "v2"
    assert brief["cards"][0]["not_generated"] == ["seventh_probe"]


def test_repetitive_source_metadata_does_not_expand_into_full_policy_packet():
    company = card()
    company["context"]["external_assessment"]["comparisons"] = [
        {
            "kind": "quants_scores",
            "origin": "same-origin",
            "metric": f"metric_{index}",
            "state": "missing",
            "latest_value": None,
            "previous_value": None,
            "comparable": False,
            "reason": "unavailable",
            "source_url": "https://source/" + "x" * 300,
            "fetched_at_utc": "2026-10-28T12:00:00Z",
        }
        for index in range(27)
    ]
    source = {
        "policy": {"method": "best-economic-path-v1"},
        "eligible_count": 36,
        "cards": [deepcopy(company) for _ in range(36)],
    }
    before = deepcopy(source)
    brief = economic_screening_brief(source)

    def size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())

    assert size(brief) < size(source) * 0.4
    assert json.dumps(brief).count('"source_url"') == 36  # metricごとではなく系列ごとに一度。
    assert brief["policy"] == source["policy"]
    for current, stored in zip(brief["cards"], source["cards"], strict=True):
        for key in ("ticker", "rank", "priority", "strongest_path"):
            assert current[key] == stored[key]
        for path, original in zip(current["paths"], stored["paths"], strict=True):
            for key in (
                "path_id",
                "variant",
                "score",
                "price_value",
                "business_value",
                "price_percentile",
                "business_percentile",
                "price_population",
                "business_population",
            ):
                assert path[key] == original[key]
    assert source == before


def test_distinct_source_urls_never_overwrite_same_metric_levels():
    company = card()
    company["context"]["external_assessment"]["comparisons"] = [
        {
            "kind": "model_valuations",
            "distributor": "same",
            "origin": "same",
            "metric": "target",
            "state": "first_observation",
            "comparable": False,
            "target_period": "FY2027",
            "latest_value": value,
            "source_url": url,
        }
        for value, url in ((100.0, "https://source/a"), (200.0, "https://source/b"))
    ]
    external = economic_screening_brief(result(company))["cards"][0]["context"][
        "external_assessment"
    ]
    assert len(external["sources"]) == 2
    assert [group["states"][0]["metrics"]["target"] for group in external["sources"]] == [
        100.0,
        200.0,
    ]


def test_reported_flows_and_independent_cfo_interval_survive_l3_brief():
    company = card()
    financial = company["context"]["financial"]
    financial["edinet_reported_flows"] = {
        "period_kind": "annual",
        "amounts": {"free_cash_flow": 3_222_500_000.0},
        "measurement_period": {"start": None, "end": None, "reason": "fact_dates_not_retained"},
        "filing_fiscal_period": {"start": "2025-04-01", "end": "2026-03-31"},
        "annual_quality": {"free_cash_flow": "exact"},
    }
    financial["ocf_ttm_period"] = {"start": "2025-07-01", "end": "2026-06-30"}
    financial["ocf_ttm"] = -1_229_635_000.0
    brief = economic_screening_brief(result(company))["cards"][0]["context"]["financial"]
    assert brief["edinet_reported_flows"] == financial["edinet_reported_flows"]
    assert brief["ocf_ttm_period"] == financial["ocf_ttm_period"]
    assert brief["ocf_ttm"] == financial["ocf_ttm"]
