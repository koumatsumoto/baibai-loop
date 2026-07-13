from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from tools.candidate_report.render import ReportError, render


def _write(path: Path, doc: object) -> None:
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")


def _fixture(
    tmp_path: Path, *, narratives: dict[str, object], asof: object = "2026-07-10"
) -> dict[str, Path]:
    selection = tmp_path / "selection-output.yaml"
    candidates = tmp_path / "candidates.yaml"
    narratives_path = tmp_path / "narratives.yaml"
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
                    },
                }
            ]
        },
    )
    _write(narratives_path, narratives)
    return {"selection": selection, "candidates": candidates, "narratives": narratives_path}


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
    assert "7/10終値" in html
    assert "+50%" in html  # (3000/2000 - 1) * 100
    assert "basis=forecast_annual" in html
    assert "割安仮説" in html
    assert "反対仮説" in html
    # Exclusion reasons render with the ticker's name pulled from candidates.
    assert "6417" in html
    assert "投資対象外" in html
    # Missing metrics render as an em dash, never a literal None.
    assert "None" not in html


def test_render_uses_selection_asof_for_close_label(tmp_path: Path) -> None:
    paths = _fixture(
        tmp_path,
        narratives={"candidates": [{"ticker": "7203", "ploss": "低", "prov": "x"}]},
        asof="2026-07-13",
    )

    html = render(**paths)

    assert "7/13終値" in html
    assert "7/10終値" not in html


@pytest.mark.parametrize("asof", [None, "not-a-date"])
def test_render_falls_back_to_undated_close_label(tmp_path: Path, asof: object) -> None:
    paths = _fixture(
        tmp_path,
        narratives={"candidates": [{"ticker": "7203", "ploss": "低", "prov": "x"}]},
        asof=asof,
    )

    html = render(**paths)

    assert "<th>終値</th>" in html
    assert "7/10終値" not in html


def test_render_falls_back_when_selection_asof_is_missing(tmp_path: Path) -> None:
    paths = _fixture(
        tmp_path,
        narratives={"candidates": [{"ticker": "7203", "ploss": "低", "prov": "x"}]},
    )
    selection_doc = yaml.safe_load(paths["selection"].read_text(encoding="utf-8"))
    selection_doc["selection"].pop("asof")
    _write(paths["selection"], selection_doc)

    html = render(**paths)

    assert "<th>終値</th>" in html
    assert "7/10終値" not in html


def test_render_rejects_ticker_absent_from_audit_pool(tmp_path: Path) -> None:
    paths = _fixture(
        tmp_path,
        narratives={"candidates": [{"ticker": "9999", "ploss": "低", "prov": "x"}]},
    )
    with pytest.raises(ReportError):
        render(**paths)
