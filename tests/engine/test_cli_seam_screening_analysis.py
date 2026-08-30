from __future__ import annotations

from pathlib import Path

import yaml

from baibai_engine.read_api import list_research_triage_payloads
from baibai_engine.screening.cli import main as screening_main
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.rules_identity import production_rules_contract_hash
from baibai_engine.screening.run_store import ScreeningRunReader, ScreeningRunStore

ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = ROOT / "method/screening/rules/2026-07-06T000000+0900.yaml"


def _analysis(ticker: str) -> dict[str, object]:
    return {
        "ticker": ticker,
        "name": ticker,
        "sector_33": "機械",
        "market_cap_oku": 500,
        "avg_turnover_oku": 5.0,
        "listing_span_days": 1000,
        "jpx_flags": [],
        "per_forward": 10.0,
        "per_trailing": 11.0,
        "pbr": 0.8,
        "p_s": 1.0,
        "ev_ebitda": 5.0,
        "pcfr": 8.0,
        "metrics": {
            "per_forward_sector_gap": -0.5,
            "normalized_per_3fy": 10.0,
            "fcf_yield": 0.08,
            "ocf_yield": 0.1,
            "asset_backed_ratio": 0.5,
            "net_cash_to_market_cap": 0.25,
            "pbr_sector_gap": -0.3,
            "equity_ratio": 0.6,
            "p_s_sector_gap": -0.4,
            "sales_yoy": 0.05,
            "operating_profit": 12.0,
            "sales_ttm": 100.0,
            "total_assets": 200.0,
            "debt": 20.0,
            "cash": 30.0,
            "er_annual": 0.13,
        },
    }


def _publish_run(path: Path) -> None:
    rules_hash = production_rules_contract_hash(load_screening_rules(RULES_PATH).model_dump_json())
    ScreeningRunStore(path).publish_run(
        {
            "run_id": "screening-20260708",
            "run_date": "2026-07-08",
            "asof_date": "2026-07-08",
            "run_at": "2026-07-08T18:00:00+09:00",
            "universe_size": 2,
            "screening_rules_hash": rules_hash,
            "er_model_version": "v1",
            "rules_ref": str(RULES_PATH),
            "security_analyses": [_analysis("1111"), _analysis("2222")],
        },
        run_revision_id="run-revision-cli-seam",
    )


def test_review_set_publish_cli_persists_the_exact_output(tmp_path: Path) -> None:
    runs_db = tmp_path / "runs.sqlite"
    output = tmp_path / "review-set.yaml"
    _publish_run(runs_db)
    assert (
        screening_main(
            [
                "review-set",
                "publish",
                "--asof",
                "2026-07-08",
                "--run-revision-id",
                "run-revision-cli-seam",
                "--runs-db",
                str(runs_db),
                "--rules-path",
                str(RULES_PATH),
                "--review-cap",
                "20",
                "--output-path",
                str(output),
            ]
        )
        == 0
    )
    emitted = yaml.safe_load(output.read_text(encoding="utf-8"))
    stored = ScreeningRunReader(runs_db).get_review_set(emitted["review_set_id"])
    assert stored is not None
    assert stored.payload == emitted


def test_research_triage_publish_cli_binds_every_review_set_entry(tmp_path: Path) -> None:
    runs_db = tmp_path / "runs.sqlite"
    output = tmp_path / "review-set.yaml"
    _publish_run(runs_db)
    assert (
        screening_main(
            [
                "review-set",
                "publish",
                "--asof",
                "2026-07-08",
                "--run-revision-id",
                "run-revision-cli-seam",
                "--runs-db",
                str(runs_db),
                "--rules-path",
                str(RULES_PATH),
                "--review-cap",
                "20",
                "--output-path",
                str(output),
            ]
        )
        == 0
    )
    review_set = yaml.safe_load(output.read_text(encoding="utf-8"))
    entries = []
    for priority, item in enumerate(review_set["entries"], start=1):
        entries.append(
            {
                "ticker": item["ticker"],
                "decision": "research",
                "priority": priority,
                "rationale": "fundamental research is warranted",
                "research_question": "durability?",
                "key_risk": "cyclicality",
                "machine_snapshot": None,
            }
        )
    draft = {
        "schema_version": 1,
        "kind": "research_triage",
        "research_triage_id": "research-triage-20260708-cli-seam",
        "review_set_id": review_set["review_set_id"],
        "run_revision_id": "run-revision-cli-seam",
        "as_of": "2026-07-08",
        "published_at": "2026-07-08T19:00:00+09:00",
        "macro_context_id": None,
        "review_basis_research_triage_id": None,
        "triage_contract_id": "research-triage-v1",
        "entries": entries,
    }
    draft_path = tmp_path / "triage.yaml"
    draft_path.write_text(yaml.safe_dump(draft, sort_keys=False), encoding="utf-8")
    app_db = tmp_path / "app.sqlite"
    assert (
        screening_main(
            [
                "research-triage",
                "publish",
                str(draft_path),
                "--db",
                str(app_db),
                "--runs-db",
                str(runs_db),
            ]
        )
        == 0
    )
    assert list_research_triage_payloads(app_db)[0]["review_set_id"] == review_set["review_set_id"]
