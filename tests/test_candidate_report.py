from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from tools.candidate_report.render import ReportError, render


def _write(path: Path, doc: object) -> None:
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")


def _fixture(
    tmp_path: Path,
    *,
    narratives: dict[str, object],
    asof: object = "2026-07-10",
    portfolio_annotation: str = "unheld",
) -> dict[str, Path]:
    selection = tmp_path / "selection-output.yaml"
    candidates = tmp_path / "candidates.yaml"
    narratives_path = tmp_path / "narratives.yaml"
    prepared = tmp_path / "selection.yaml"
    _write(
        selection,
        {
            "selection": {"asof": asof},
            "audit_pool": [
                {
                    "rank": 1,
                    "ticker": "7203",
                    "name": "トヨタ",
                    "expected_return_pct": 9.5,
                    "fair_value_anchor_yen": 3000.0,
                    "market_price_yen": 2000.0,
                    "event_warnings": [],
                    "liquidity_status": "pass",
                }
            ],
        },
    )
    _write(
        candidates,
        {
            "candidates": [
                {
                    "ticker": "7203",
                    "name": "トヨタ",
                    "sector_33": "輸送用機器",
                    "market_cap_oku": 1234,
                    "per_forward": 10.0,
                    "per_trailing": 12.0,
                    "pbr": 1.1,
                    "ev_ebitda": 6.0,
                    "price_change_60d": -0.062,
                    "gap_from_52w_low": 0.088,
                    "avg_turnover_oku": 25.5,
                    "next_earnings_date": "2026-08-07",
                    "freshness_warnings": [],
                    "ttm_quality": {"per_forward": "exact"},
                    "metrics": {
                        "equity_ratio": 0.6,
                        "net_cash_to_market_cap": 0.2,
                        "ocf_yield": 0.08,
                        "fcf_yield": 0.05,
                        "dps_actual_annual": 60.0,
                        "dps_forecast_annual": 66.0,
                        "dividend_yield": 0.033,
                        "dividend_basis": "forecast_annual",
                        "dividend_split_factor": None,
                        "er_reversion_annual": 0.021,
                        "er_carry_annual": 0.098,
                        "sales_yoy": 0.032,
                        "operating_profit_yoy": -0.045,
                        "fv_self_range_yen": 2900.0,
                        "fv_sector_median_yen": 3100.0,
                        "er_anchor_metrics": "pbr,per_forward",
                        "edinet_failure_reasons": None,
                        "bs_carry_forward_lag_days": 0,
                    },
                }
            ]
        },
    )
    _write(narratives_path, narratives)
    _write(
        prepared,
        {
            "as_of": asof,
            "audit_pool": [{"ticker": "7203", "portfolio_annotation": portfolio_annotation}],
        },
    )
    return {
        "selection": selection,
        "candidates": candidates,
        "narratives": narratives_path,
        "prepared": prepared,
    }


def test_render_pulls_numbers_from_screening_output(tmp_path: Path) -> None:
    paths = _fixture(
        tmp_path,
        narratives={
            "meta": {"title": "テスト", "target_session": "2026-07-13"},
            "candidates": [
                {
                    "ticker": "7203",
                    "ploss": "低",
                    "prov": "有力",
                    "why": "割安仮説",
                    "temporary": "一時的要因",
                    "structural": "構造的要因",
                    "survive": "耐性",
                    "unlock": "改善要因",
                    "counter": "反対仮説",
                    "research": "確認事項",
                    "value": "深掘り価値",
                }
            ],
            "excluded": [{"ticker": "6417", "reason": "投資対象外"}],
        },
    )
    html = render(**paths)
    # Hard numbers come from the screening output, and the FV gap is computed.
    assert "TSE%3A7203" in html
    assert "2,000.0 円" in html
    assert "7/10 screening参考価格" in html
    assert "+50%" in html  # (3000/2000 - 1) * 100
    assert "basis=forecast_annual" in html
    assert "割安仮説" in html
    assert "反対仮説" in html
    assert "Content-Security-Policy" in html
    assert "script-src 'none'" in html
    # Exclusion reasons render with the ticker's name pulled from candidates.
    assert "6417" in html
    assert "投資対象外" in html
    # Missing metrics render as an em dash, never a literal None.
    assert "None" not in html


def test_render_uses_selection_asof_for_price_label(tmp_path: Path) -> None:
    paths = _fixture(
        tmp_path,
        narratives={"candidates": [{"ticker": "7203", "ploss": "低", "prov": "x"}]},
        asof="2026-07-13",
    )

    html = render(**paths)

    assert "7/13 screening参考価格" in html
    assert "7/10 screening参考価格" not in html


@pytest.mark.parametrize("asof", [None, "not-a-date"])
def test_render_falls_back_to_undated_close_label(tmp_path: Path, asof: object) -> None:
    paths = _fixture(
        tmp_path,
        narratives={"candidates": [{"ticker": "7203", "ploss": "低", "prov": "x"}]},
        asof=asof,
    )

    html = render(**paths)

    assert "<th>screening参考価格</th>" in html
    assert "7/10 screening参考価格" not in html


def test_render_falls_back_when_selection_asof_is_missing(tmp_path: Path) -> None:
    paths = _fixture(
        tmp_path,
        narratives={"candidates": [{"ticker": "7203", "ploss": "低", "prov": "x"}]},
    )
    selection_doc = yaml.safe_load(paths["selection"].read_text(encoding="utf-8"))
    selection_doc["selection"].pop("asof")
    _write(paths["selection"], selection_doc)

    html = render(**paths)

    assert "<th>screening参考価格</th>" in html
    assert "7/10 screening参考価格" not in html


def test_render_rejects_ticker_absent_from_audit_pool(tmp_path: Path) -> None:
    paths = _fixture(
        tmp_path,
        narratives={"candidates": [{"ticker": "9999", "ploss": "低", "prov": "x"}]},
    )
    with pytest.raises(ReportError):
        render(**paths)


def test_render_shows_held_and_reserved_annotation_from_prepared_selection(
    tmp_path: Path,
) -> None:
    paths = _fixture(
        tmp_path,
        narratives={"candidates": [{"ticker": "7203", "ploss": "低", "prov": "x"}]},
        portfolio_annotation="held_and_reserved",
    )

    html = render(**paths)

    assert "保有中＋予約中" in html
    assert "未保有" not in html


def test_render_rejects_ticker_absent_from_prepared_selection(tmp_path: Path) -> None:
    paths = _fixture(
        tmp_path,
        narratives={"candidates": [{"ticker": "7203", "ploss": "低", "prov": "x"}]},
    )
    prepared_doc = yaml.safe_load(paths["prepared"].read_text(encoding="utf-8"))
    prepared_doc["audit_pool"] = []
    _write(paths["prepared"], prepared_doc)

    with pytest.raises(ReportError):
        render(**paths)


def test_render_shows_undated_earnings_label_when_next_earnings_date_is_missing(
    tmp_path: Path,
) -> None:
    paths = _fixture(
        tmp_path,
        narratives={"candidates": [{"ticker": "7203", "ploss": "低", "prov": "x"}]},
    )
    candidates_doc = yaml.safe_load(paths["candidates"].read_text(encoding="utf-8"))
    candidates_doc["candidates"][0]["next_earnings_date"] = None
    _write(paths["candidates"], candidates_doc)

    html = render(**paths)

    assert "未定/JPX未公表" in html


def test_render_shows_no_data_quality_warnings_when_all_metrics_are_clean(
    tmp_path: Path,
) -> None:
    paths = _fixture(
        tmp_path,
        narratives={"candidates": [{"ticker": "7203", "ploss": "低", "prov": "x"}]},
    )

    html = render(**paths)

    assert "<th>データ品質</th><td>なし</td>" in html


def test_render_shows_data_quality_warnings_for_non_exact_ttm_and_bs_carry_lag(
    tmp_path: Path,
) -> None:
    paths = _fixture(
        tmp_path,
        narratives={"candidates": [{"ticker": "7203", "ploss": "低", "prov": "x"}]},
    )
    candidates_doc = yaml.safe_load(paths["candidates"].read_text(encoding="utf-8"))
    cand = candidates_doc["candidates"][0]
    cand["ttm_quality"] = {"per_forward": "exact", "fcf_yield": "unavailable"}
    cand["metrics"]["bs_carry_forward_lag_days"] = 92
    _write(paths["candidates"], candidates_doc)

    html = render(**paths)

    assert "ttm非exact: fcf_yield" in html
    assert "BS前期繰越 92日" in html


def test_render_shows_er_decomposition_and_growth_percentages_from_metrics(
    tmp_path: Path,
) -> None:
    paths = _fixture(
        tmp_path,
        narratives={"candidates": [{"ticker": "7203", "ploss": "低", "prov": "x"}]},
    )

    html = render(**paths)

    # metrics.er_reversion_annual=0.021, er_carry_annual=0.098 (see _fixture).
    assert "+2.1" in html
    assert "+9.8" in html
    # cand.price_change_60d=-0.062 rendered as a signed percentage.
    assert "-6.2" in html
    # metrics.sales_yoy=0.032, operating_profit_yoy=-0.045.
    assert "+3.2" in html
    assert "-4.5" in html
