from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import yaml

from baibai_engine.read_api import list_research_triage_payloads
from baibai_engine.research.workspace import _screening_estimate_from_triage_snapshot
from baibai_engine.screening.cli import main as screening_main
from baibai_engine.screening.research_triage import ResearchTriageCandidateSnapshot
from baibai_engine.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules
from baibai_engine.screening.rules_identity import production_rules_contract_hash
from baibai_engine.screening.run_store import ScreeningRunReader, ScreeningRunStore

ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = ROOT / DEFAULT_RULES_PATH


def _analysis(
    ticker: str,
    *,
    er_annual: float | None = 0.13,
    stale_fin_flag: bool | None = None,
) -> dict[str, object]:
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
            "er_annual": er_annual,
            "er_origin": "estimate",
            "er_model_version": "v1",
            "er_unit": "annual_ratio",
            "er_assumptions": "fixture assumptions",
            "stale_fin_flag": stale_fin_flag,
        },
    }


def _publish_run(path: Path, *, include_unknown_triage_input: bool = False) -> None:
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
            "security_analyses": [
                _analysis(
                    "1111",
                    er_annual=None if include_unknown_triage_input else 0.13,
                    stale_fin_flag=True if include_unknown_triage_input else None,
                ),
                _analysis("2222"),
            ],
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


def test_review_set_show_json_is_one_machine_readable_object(tmp_path: Path, capsys) -> None:
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
                "--output-path",
                str(output),
            ]
        )
        == 0
    )
    review_set_id = yaml.safe_load(output.read_text(encoding="utf-8"))["review_set_id"]
    capsys.readouterr()

    assert (
        screening_main(
            [
                "review-set",
                "show",
                "--review-set-id",
                review_set_id,
                "--runs-db",
                str(runs_db),
                "--format",
                "json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["review_set_id"] == review_set_id


def test_review_set_preallocated_identity_is_idempotent_for_resume(tmp_path: Path, capsys) -> None:
    runs_db = tmp_path / "runs.sqlite"
    _publish_run(runs_db)
    argv = [
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
        "--review-set-id",
        "review-set-20260708-resume",
        "--created-at",
        "2026-07-08T10:00:00+00:00",
        "--format",
        "json",
    ]

    assert screening_main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert screening_main(argv) == 0
    second = json.loads(capsys.readouterr().out)

    assert first == second
    assert (
        len(ScreeningRunReader(runs_db).list_review_sets(run_revision_id="run-revision-cli-seam"))
        == 1
    )


def test_review_set_composition_is_independent_of_application_state(tmp_path: Path) -> None:
    runs_db = tmp_path / "runs.sqlite"
    _publish_run(runs_db)

    def publish() -> dict[str, object]:
        output = tmp_path / f"review-set-{len(list(tmp_path.glob('review-set-*.yaml')))}.yaml"
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
                    "--output-path",
                    str(output),
                ]
            )
            == 0
        )
        return yaml.safe_load(output.read_text(encoding="utf-8"))

    first = publish()
    app_db = tmp_path / "application.sqlite"
    app_db.write_bytes(b"unrelated application state")
    second = publish()

    assert {
        key: first[key] for key in ("method", "entries", "screening_rules_hash", "diagnostics")
    } == {key: second[key] for key in ("method", "entries", "screening_rules_hash", "diagnostics")}


def test_research_triage_publish_cli_binds_every_review_set_entry(tmp_path: Path) -> None:
    runs_db = tmp_path / "runs.sqlite"
    output = tmp_path / "review-set.yaml"
    _publish_run(runs_db, include_unknown_triage_input=True)
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
                "--output-path",
                str(output),
            ]
        )
        == 0
    )
    review_set = yaml.safe_load(output.read_text(encoding="utf-8"))
    draft_path = tmp_path / "triage.yaml"
    app_db = tmp_path / "app.sqlite"
    assert (
        screening_main(
            [
                "research-triage",
                "scaffold",
                "--review-set-id",
                review_set["review_set_id"],
                "--runs-db",
                str(runs_db),
                "--output-path",
                str(draft_path),
                "--db",
                str(app_db),
            ]
        )
        == 0
    )
    draft = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    draft["research_triage_id"] = "research-triage-20260708-cli-seam"
    draft["published_at"] = "2026-07-08T19:00:00+09:00"
    for priority, entry in enumerate(draft["entries"], start=1):
        entry.update(
            {
                "decision": "research",
                "priority": priority,
                "rationale": "fundamental research is warranted",
                "research_question": "durability?",
                "key_risk": "cyclicality",
            }
        )
    draft_path.write_text(yaml.safe_dump(draft, sort_keys=False), encoding="utf-8")
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
    published = list_research_triage_payloads(app_db)[0]
    assert published["review_set_id"] == review_set["review_set_id"]
    unknown_entry = next(entry for entry in published["entries"] if entry["ticker"] == "1111")
    assert unknown_entry["decision"] == "research"
    snapshot = unknown_entry["candidate_snapshot"]
    assert snapshot["analysis"]["expected_return"]["er_annual"] is None
    assert snapshot["analysis"]["data_quality"]["stale_fin_flag"] is True


def test_research_triage_scaffold_carries_machine_coordinates_and_fails_closed(
    tmp_path: Path,
) -> None:
    runs_db = tmp_path / "runs.sqlite"
    review_set_path = tmp_path / "review-set.yaml"
    draft_output = tmp_path / "research-triage.yaml"
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
                "--output-path",
                str(review_set_path),
            ]
        )
        == 0
    )
    assert (
        screening_main(
            [
                "research-triage",
                "scaffold",
                "--review-set-id",
                yaml.safe_load(review_set_path.read_text(encoding="utf-8"))["review_set_id"],
                "--runs-db",
                str(runs_db),
                "--output-path",
                str(draft_output),
                "--db",
                str(tmp_path / "app.sqlite"),
            ]
        )
        == 0
    )

    review_set = yaml.safe_load(review_set_path.read_text(encoding="utf-8"))
    draft = yaml.safe_load(draft_output.read_text(encoding="utf-8"))
    assert [entry["ticker"] for entry in draft["entries"]] == [
        entry["ticker"] for entry in review_set["entries"]
    ]
    assert draft["entries"][0]["decision"] == "TODO"
    assert draft["entries"][0]["rationale"].startswith("TODO")
    assert (
        draft["entries"][0]["candidate_snapshot"]["nominations"]
        == review_set["entries"][0]["nominations"]
    )
    parsed_snapshot = ResearchTriageCandidateSnapshot.model_validate(
        draft["entries"][0]["candidate_snapshot"]
    )
    assert [item.model_dump(mode="json") for item in parsed_snapshot.nominations] == review_set[
        "entries"
    ][0]["nominations"]
    assert (
        screening_main(
            [
                "research-triage",
                "publish",
                str(draft_output),
                "--db",
                str(tmp_path / "app.sqlite"),
                "--runs-db",
                str(runs_db),
            ]
        )
        == 1
    )


def test_thesis_scaffold_screening_estimate_names_its_local_source(tmp_path: Path) -> None:
    runs_db = tmp_path / "runs.sqlite"
    review_set_path = tmp_path / "review-set.yaml"
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
                "--output-path",
                str(review_set_path),
            ]
        )
        == 0
    )
    review_set = yaml.safe_load(review_set_path.read_text(encoding="utf-8"))
    ticker = review_set["entries"][0]["ticker"]

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "research-workspace.yaml").write_text(
        yaml.safe_dump(
            {"as_of": "2026-07-08", "candidates": review_set["entries"]}, sort_keys=False
        ),
        encoding="utf-8",
    )
    estimate, reason = _screening_estimate_from_triage_snapshot(
        workspace=workspace,
        ticker=ticker,
        asof=date(2026, 7, 8),
    )

    assert reason is None
    assert estimate is not None
    assert estimate["source_ids"] == ["screening_analysis"]
