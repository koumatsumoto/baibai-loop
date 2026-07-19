from __future__ import annotations

import hashlib
import shutil
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from tools.research_decision_report.render import (
    ReportError,
    _external_link,
    render,
    review_bindings,
)
from tools.research_decision_report.review_scaffold import scaffold

from baibai_engine.research.decision_packet import (
    decision_packet_core_hash,
    load_decision_packet,
)
from baibai_engine.research.execution_policy import max_acceptable_price

FIXTURES = Path(__file__).parent / "fixtures" / "decision-packet"
REQUIRED_REVIEW_CHECKS = (
    "source_freshness",
    "user_questions_answered",
    "primary_source_traceability",
    "fact_estimate_separation",
    "countercase_and_unknowns",
    "scenario_and_fv_consistency",
    "comparison_and_portfolio_fit",
    "purchase_method_binding",
)


def _write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_DEFAULT_ENTRY_TIMING = (
    "次のmaterial eventは2026-08-05のFY2026通期決算。指値は前営業日終値でFV比+25.97%の余地があり、"
    "決算前でも要求利回りを満たすため待たずに提案する。"
)


def _findings(*, entry_timing: str | None = _DEFAULT_ENTRY_TIMING) -> dict[str, object]:
    primary = {
        "statement": "警備契約の継続収入と省人化が価値獲得の中心である。",
        "kind": "observed",
        "source_ids": ["primary-results"],
    }
    return {
        "schema_version": 1,
        "meta": {
            "title": "2331 統合調査レポート",
            "as_of": "2026-07-03",
            "target_session": "2026-07-06",
            "budget_yen": 300_000,
            "source_freshness": "決算資料と市場終値を2026-07-03時点で確認",
            "warnings": ["顧客集中度は未開示"],
        },
        "decision_context": {
            "portfolio_fit": "既存保有と異なる需要源を追加する。",
            "human_action": "指値と数量を確認し、発注可否を最終判断する。",
            "entry_timing": entry_timing,
        },
        "candidates": [
            {
                "ticker": "2331",
                "confidence": "medium",
                "assigned_questions": [
                    {
                        "heading": "既存収益で投資を賄えるか",
                        "conclusion": "営業CFが投資負担を吸収できる。",
                        "evidence": [primary],
                    }
                ],
                "business_model": primary,
                "value_capture": {
                    **primary,
                    "statement": "契約単価と自動化余力を利益率へ転換する。",
                    "kind": "derived",
                },
                "growth_quality": [
                    {
                        "heading": "成長分解",
                        "conclusion": "契約量、価格、省人化を分けて監視する。",
                        "evidence": [primary],
                    }
                ],
                "financial_resilience": {
                    **primary,
                    "statement": "営業CFは正で、ストレス期間の資金を賄える。",
                },
                "domain_findings": [],
                "strongest_countercase": {
                    **primary,
                    "statement": "買収後のFCF conversionが利益成長に追随しない可能性がある。",
                    "kind": "estimate",
                },
                "catalysts": [
                    {
                        "expected_on": None,
                        "event": "次回決算",
                        "decision_impact": "利益率とFCF conversionを再評価する。",
                    },
                    {
                        "expected_on": "2026-09-01",
                        "event": "中間配当基準日",
                        "decision_impact": "配当継続方針を確認する。",
                    },
                    {
                        "expected_on": "2026-08-05",
                        "event": "FY2026通期決算",
                        "decision_impact": "通期業績と自動化投資計画を確認する。",
                    },
                ],
                "unknowns": ["最大顧客の売上比率は未開示"],
                "monitoring": ["営業CFと設備投資の推移"],
                "source_metadata": [
                    {
                        "source_id": "primary-results",
                        "document_title": "ALSOK 決算・IR資料",
                        "published_at": "2026-06-30",
                        "status": "ok",
                        "note": None,
                    }
                ],
            }
        ],
    }


def _comparison(*, selected_ticker: str | None) -> dict[str, object]:
    return {
        "selected_ticker": selected_ticker,
        "ranking_rationale": "永久損失耐性と5年期待収益を統合して判断した。",
        "candidates": [
            {
                "ticker": "2331",
                "temporary_mispricing_hypothesis": "短期費用を構造悪化と誤認している。",
                "permanent_loss_conclusion": "顧客集中のみ未確認で、その他は許容範囲。",
                "five_year_base_cagr_pct": 9.57,
                "fair_value_yen": 1_300,
                "fv_gap_pct": 25.97,
                "portfolio_marginal_value": "需要源の分散に寄与する。",
                "strongest_countercase": "買収投資でFCFが低迷する。",
                "disposition": "select" if selected_ticker else "defer",
                "disposition_reason": (
                    "期待収益と耐性を両立する。"
                    if selected_ticker
                    else "要求収益を満たす指値を作れない。"
                ),
            }
        ],
    }


def _proposal(workspace: Path, *, status: str) -> dict[str, object]:
    packet_path = workspace / "2331" / "packet-draft.yaml"
    review_path = workspace / "2331" / "review-draft.yaml"
    packet = load_decision_packet(packet_path)
    manifest = yaml.safe_load((workspace / "manifest.yaml").read_text(encoding="utf-8"))
    planned = status == "planned_limit"
    assert packet.estimates is not None
    close_yen = int(packet.estimates.entry_price_basis_yen)
    quantity = 200 if planned else 0
    proposal: dict[str, object] = {
        "status": status,
        "ticker": "2331",
        "decision_packet_sha256": _sha256(packet_path),
        "decision_packet_core_sha256": decision_packet_core_hash(packet),
        "independent_review_sha256": _sha256(review_path),
        "source_ledger_sha256": manifest["inputs"]["ledger"]["sha256"],
        "expires_at": "2026-07-06T15:30:00+09:00",
        "price_as_of": "2026-07-03",
        "price_basis": "last_close_unadjusted",
        "close_yen": close_yen,
        "max_acceptable_price_yen": int(max_acceptable_price(packet, tick_size_yen=Decimal(1))),
        "board_lot": 100,
        "budget_min_yen": 200_000,
        "budget_max_yen": 300_000,
        "portfolio_annotations": ["ledger_warning:portfolio.ticker-concentration"],
        "limit_price_yen": close_yen if planned else None,
        "quantity": quantity,
        "notional_yen": close_yen * quantity,
        "warnings": (
            [
                "dry_powder_below_floor",
                "prospective_ticker_concentration_exceeds_warning",
                "prospective_common_factor_concentration_exceeds_warning:labor-automation",
                "portfolio_exposure_ledger_fallback:9999",
            ]
            if planned
            else []
        ),
        "defer_reasons": [] if planned else ["close_above_max_acceptable_price"],
    }
    if planned:
        notional = close_yen * quantity
        proposal["portfolio_exposure"] = {
            "price_as_of": "2026-07-03",
            "price_basis": "last_close_unadjusted",
            "holding_valuation_status": "mixed_with_ledger_fallback",
            "total_capital_yen": 5_000_000,
            "ledger_fallback_tickers": ["9999"],
            "common_factor_empty_tickers": [],
            "ticker": {
                "key": "2331",
                "current_and_reserved_yen": 100_000,
                "prospective_yen": 100_000 + notional,
                "prospective_pct": 6.13,
                "warning_pct": 6.0,
            },
            "sector": {
                "key": "サービス業",
                "current_and_reserved_yen": 1_000_000,
                "prospective_yen": 1_000_000 + notional,
                "prospective_pct": 24.13,
                "warning_pct": 40.0,
            },
            "common_factors": [
                {
                    "key": "labor-automation",
                    "current_and_reserved_yen": 1_900_000,
                    "prospective_yen": 1_900_000 + notional,
                    "prospective_pct": 42.13,
                    "warning_pct": 35.0,
                }
            ],
        }
    return proposal


def _review(
    *,
    workspace: Path,
    findings_path: Path,
    proposal_path: Path | None,
    selected_ticker: str | None,
) -> dict[str, object]:
    packet_path = workspace / "2331" / "packet-draft.yaml"
    packet = load_decision_packet(packet_path)
    bindings = review_bindings(
        workspace=workspace,
        findings_path=findings_path,
        proposal_path=proposal_path,
        selected_ticker=selected_ticker,
        packet_paths={"2331": packet_path},
        packets={"2331": packet},
    )
    return {
        "schema_version": 1,
        "reviewer_role": "independent_second_pass",
        "reviewer_identity": "independent-report-reviewer",
        "reviewer_run_id": "report-review-2331-20260703",
        "reviewed_at": datetime.fromisoformat("2026-07-03T12:00:00+09:00"),
        "conclusion": "pass",
        "reviewed_inputs": bindings,
        "checks": [
            {"check_id": check_id, "status": "pass", "note": "確認済み"}
            for check_id in REQUIRED_REVIEW_CHECKS
        ],
        "findings": [],
    }


def _workspace(
    tmp_path: Path,
    *,
    mode: str = "planned_limit",
    entry_timing: str | None = _DEFAULT_ENTRY_TIMING,
) -> dict[str, Path | None]:
    workspace = tmp_path / "opportunity"
    ticker_dir = workspace / "2331"
    ticker_dir.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "2331-decision.yaml", ticker_dir / "packet-draft.yaml")
    shutil.copyfile(FIXTURES / "2331-decision-review.yaml", ticker_dir / "review-draft.yaml")
    selection_input = workspace / "inputs" / "selection-output.yaml"
    ledger_input = workspace / "inputs" / "ledger.yaml"
    _write(selection_input, {"run_id": "fixture-selection"})
    _write(ledger_input, {"schema_version": 1, "fixture": "ledger"})
    _write(
        workspace / "manifest.yaml",
        {
            "as_of": "2026-07-03",
            "inputs": {
                "selection_output": {
                    "path": str(selection_input),
                    "sha256": _sha256(selection_input),
                },
                "ledger": {"path": str(ledger_input), "sha256": _sha256(ledger_input)},
            },
        },
    )
    _write(workspace / "selection.yaml", {"shortlist": [{"ticker": "2331"}]})
    selected_ticker = None if mode == "no_selected" else "2331"
    _write(
        workspace / "research-comparison.yaml",
        _comparison(selected_ticker=selected_ticker),
    )
    findings_path = workspace / "findings.yaml"
    _write(findings_path, _findings(entry_timing=entry_timing))
    proposal_path: Path | None = None
    if selected_ticker is not None:
        proposal_path = workspace / "plan-limit.yaml"
        _write(proposal_path, _proposal(workspace, status=mode))
    review_path = workspace / "report-review.yaml"
    _write(
        review_path,
        _review(
            workspace=workspace,
            findings_path=findings_path,
            proposal_path=proposal_path,
            selected_ticker=selected_ticker,
        ),
    )
    return {
        "workspace": workspace,
        "findings_path": findings_path,
        "review_path": review_path,
        "proposal_path": proposal_path,
    }


def test_render_planned_limit_after_passing_hash_bound_review(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)

    document = render(**paths)  # type: ignore[arg-type]

    assert "推奨する購入方法" in document
    assert "1,032.0円" in document
    assert "200株" in document
    assert "206,400円" in document
    assert "内容レビュー: pass" in document
    assert "independent-report-reviewer" in document
    assert "ledger_warning:portfolio.ticker-concentration" in document
    assert "portfolio exposure（同一as-of）" in document
    assert "5,000,000円" in document
    assert "6.13%" in document
    assert "24.13%" in document
    assert "42.13%" in document
    assert "ledger fallback" in document
    assert "holding valuation status" in document
    assert "mixed_with_ledger_fallback" in document
    assert "9999" in document
    assert "TSE%3A2331" in document
    assert "Content-Security-Policy" in document
    assert "script-src 'none'" in document
    assert "www.alsok.co.jp" in document


def test_render_includes_passing_review_warnings(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    review_path = paths["review_path"]
    assert isinstance(review_path, Path)
    review = yaml.safe_load(review_path.read_text(encoding="utf-8"))
    review["findings"] = [
        {
            "severity": "warning",
            "ticker": "2331",
            "section": "portfolio_fit",
            "issue": "ledger valuationの再確認が必要",
            "required_change": None,
        }
    ]
    _write(review_path, review)

    document = render(**paths)  # type: ignore[arg-type]

    assert "content review finding" in document
    assert "ledger valuationの再確認が必要" in document


def test_render_accepts_quoted_timezone_aware_reviewed_at(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    review_path = paths["review_path"]
    assert isinstance(review_path, Path)
    review = yaml.safe_load(review_path.read_text(encoding="utf-8"))
    review["reviewed_at"] = "2026-07-03T12:00:00+09:00"
    _write(review_path, review)

    document = render(**paths)  # type: ignore[arg-type]

    assert "内容レビュー: pass" in document


def test_render_rejects_naive_reviewed_at(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    review_path = paths["review_path"]
    assert isinstance(review_path, Path)
    review = yaml.safe_load(review_path.read_text(encoding="utf-8"))
    review["reviewed_at"] = "2026-07-03T12:00:00"
    _write(review_path, review)

    with pytest.raises(ReportError, match="reviewed_at must include a timezone"):
        render(**paths)  # type: ignore[arg-type]


def test_review_scaffold_binds_validated_compact_inputs(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    workspace = paths["workspace"]
    findings_path = paths["findings_path"]
    proposal_path = paths["proposal_path"]
    assert isinstance(workspace, Path)
    assert isinstance(findings_path, Path)
    assert isinstance(proposal_path, Path)

    document = scaffold(
        workspace=workspace, findings_path=findings_path, proposal_path=proposal_path
    )

    assert document["conclusion"] == "pending"
    assert document["reviewed_inputs"]["findings_sha256"] == _sha256(findings_path)
    assert {check["status"] for check in document["checks"]} == {"pending"}


def test_render_accepts_repository_relative_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    relative_paths = {
        "workspace": Path("opportunity"),
        "findings_path": Path("opportunity/findings.yaml"),
        "review_path": Path("opportunity/report-review.yaml"),
        "proposal_path": Path("opportunity/plan-limit.yaml"),
    }

    document = render(**relative_paths)

    assert "2331/packet-draft.yaml" in document


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("defer", "購入提案なし — defer"), ("no_selected", "no actionable bargain")],
)
def test_render_keeps_defer_and_no_selection_as_explicit_no_order_outcomes(
    tmp_path: Path, mode: str, expected: str
) -> None:
    paths = _workspace(tmp_path, mode=mode)

    document = render(**paths)  # type: ignore[arg-type]

    assert expected in document
    assert "推奨する購入方法" not in document


def test_render_rejects_portfolio_exposure_on_defer(tmp_path: Path) -> None:
    paths = _workspace(tmp_path, mode="defer")
    workspace = paths["workspace"]
    findings_path = paths["findings_path"]
    review_path = paths["review_path"]
    proposal_path = paths["proposal_path"]
    assert isinstance(workspace, Path)
    assert isinstance(findings_path, Path)
    assert isinstance(review_path, Path)
    assert isinstance(proposal_path, Path)
    proposal = yaml.safe_load(proposal_path.read_text(encoding="utf-8"))
    proposal["portfolio_exposure"] = {}
    _write(proposal_path, proposal)
    _write(
        review_path,
        _review(
            workspace=workspace,
            findings_path=findings_path,
            proposal_path=proposal_path,
            selected_ticker="2331",
        ),
    )

    with pytest.raises(ReportError, match="defer proposal cannot contain portfolio_exposure"):
        render(**paths)  # type: ignore[arg-type]


def test_render_rejects_review_after_a_reviewed_input_changes(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    findings_path = paths["findings_path"]
    assert isinstance(findings_path, Path)
    findings = yaml.safe_load(findings_path.read_text(encoding="utf-8"))
    findings["meta"]["warnings"].append("レビュー後に追加された警告")
    _write(findings_path, findings)

    with pytest.raises(ReportError, match="input hashes are stale"):
        render(**paths)  # type: ignore[arg-type]


def test_render_rejects_current_ledger_hash_drift(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    workspace = paths["workspace"]
    assert isinstance(workspace, Path)
    manifest = yaml.safe_load((workspace / "manifest.yaml").read_text(encoding="utf-8"))
    ledger_path = Path(manifest["inputs"]["ledger"]["path"])
    _write(ledger_path, {"schema_version": 1, "fixture": "changed-ledger"})

    with pytest.raises(ReportError, match="external input hash is stale: ledger"):
        render(**paths)  # type: ignore[arg-type]


def test_render_rejects_order_arithmetic_even_with_fresh_review(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    workspace = paths["workspace"]
    findings_path = paths["findings_path"]
    review_path = paths["review_path"]
    proposal_path = paths["proposal_path"]
    assert isinstance(workspace, Path)
    assert isinstance(findings_path, Path)
    assert isinstance(review_path, Path)
    assert isinstance(proposal_path, Path)
    proposal = yaml.safe_load(proposal_path.read_text(encoding="utf-8"))
    proposal["notional_yen"] = 199_999
    _write(proposal_path, proposal)
    _write(
        review_path,
        _review(
            workspace=workspace,
            findings_path=findings_path,
            proposal_path=proposal_path,
            selected_ticker="2331",
        ),
    )

    with pytest.raises(ReportError, match="notional must equal"):
        render(**paths)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda proposal: proposal.pop("portfolio_exposure"),
            "proposal portfolio_exposure must be a mapping",
        ),
        (
            lambda proposal: proposal.update({"portfolio_exposure": None}),
            "proposal portfolio_exposure must be a mapping",
        ),
        (
            lambda proposal: proposal["portfolio_exposure"].update({"total_capital_yen": 0}),
            "total_capital_yen must be positive",
        ),
        (
            lambda proposal: proposal["portfolio_exposure"]["ticker"].update(
                {"prospective_yen": 1}
            ),
            "prospective_yen must equal",
        ),
        (
            lambda proposal: proposal["portfolio_exposure"]["sector"].update(
                {"prospective_pct": 99.0}
            ),
            "prospective_pct does not match",
        ),
        (
            lambda proposal: proposal["portfolio_exposure"]["ticker"].update({"key": "9999"}),
            "ticker key does not match",
        ),
        (
            lambda proposal: proposal["portfolio_exposure"]["common_factors"][0].update(
                {"key": "wrong-factor"}
            ),
            "common-factor keys do not match",
        ),
        (
            lambda proposal: proposal["portfolio_exposure"]["ticker"].update({"warning_pct": 99.0}),
            "warning_pct does not match portfolio policy",
        ),
        (
            lambda proposal: proposal["portfolio_exposure"]["ticker"].update({"unknown": "bypass"}),
            "fields do not match the portfolio exposure contract",
        ),
    ],
)
def test_render_rejects_tampered_portfolio_exposure_with_fresh_review(
    tmp_path: Path, mutate: object, message: str
) -> None:
    paths = _workspace(tmp_path)
    workspace = paths["workspace"]
    findings_path = paths["findings_path"]
    review_path = paths["review_path"]
    proposal_path = paths["proposal_path"]
    assert isinstance(workspace, Path)
    assert isinstance(findings_path, Path)
    assert isinstance(review_path, Path)
    assert isinstance(proposal_path, Path)
    proposal = yaml.safe_load(proposal_path.read_text(encoding="utf-8"))
    assert callable(mutate)
    mutate(proposal)
    _write(proposal_path, proposal)
    _write(
        review_path,
        _review(
            workspace=workspace,
            findings_path=findings_path,
            proposal_path=proposal_path,
            selected_ticker="2331",
        ),
    )

    with pytest.raises(ReportError, match=message):
        render(**paths)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("remove_warning", "fallback_tickers", "message"),
    [
        (
            "prospective_ticker_concentration_exceeds_warning",
            ["9999"],
            "concentration warnings do not match thresholds",
        ),
        (
            "portfolio_exposure_ledger_fallback:9999",
            ["9999"],
            "fallback tickers and warnings do not match",
        ),
        (
            None,
            [],
            "fallback tickers and warnings do not match",
        ),
    ],
)
def test_render_rejects_portfolio_exposure_warning_mismatch_with_fresh_review(
    tmp_path: Path,
    remove_warning: str | None,
    fallback_tickers: list[str],
    message: str,
) -> None:
    paths = _workspace(tmp_path)
    workspace = paths["workspace"]
    findings_path = paths["findings_path"]
    review_path = paths["review_path"]
    proposal_path = paths["proposal_path"]
    assert isinstance(workspace, Path)
    assert isinstance(findings_path, Path)
    assert isinstance(review_path, Path)
    assert isinstance(proposal_path, Path)
    proposal = yaml.safe_load(proposal_path.read_text(encoding="utf-8"))
    if remove_warning is not None:
        proposal["warnings"].remove(remove_warning)
    proposal["portfolio_exposure"]["ledger_fallback_tickers"] = fallback_tickers
    _write(proposal_path, proposal)
    _write(
        review_path,
        _review(
            workspace=workspace,
            findings_path=findings_path,
            proposal_path=proposal_path,
            selected_ticker="2331",
        ),
    )

    with pytest.raises(ReportError, match=message):
        render(**paths)  # type: ignore[arg-type]


def test_render_rejects_holding_valuation_status_that_hides_fallback(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    workspace = paths["workspace"]
    findings_path = paths["findings_path"]
    review_path = paths["review_path"]
    proposal_path = paths["proposal_path"]
    assert isinstance(workspace, Path)
    assert isinstance(findings_path, Path)
    assert isinstance(review_path, Path)
    assert isinstance(proposal_path, Path)
    proposal = yaml.safe_load(proposal_path.read_text(encoding="utf-8"))
    proposal["portfolio_exposure"]["holding_valuation_status"] = "same_asof_raw_close"
    _write(proposal_path, proposal)
    _write(
        review_path,
        _review(
            workspace=workspace,
            findings_path=findings_path,
            proposal_path=proposal_path,
            selected_ticker="2331",
        ),
    )

    with pytest.raises(ReportError, match="holding_valuation_status does not match"):
        render(**paths)  # type: ignore[arg-type]


def test_render_rejects_unknown_common_factor_warning_with_fresh_review(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    workspace = paths["workspace"]
    findings_path = paths["findings_path"]
    review_path = paths["review_path"]
    proposal_path = paths["proposal_path"]
    assert isinstance(workspace, Path)
    assert isinstance(findings_path, Path)
    assert isinstance(review_path, Path)
    assert isinstance(proposal_path, Path)
    proposal = yaml.safe_load(proposal_path.read_text(encoding="utf-8"))
    proposal["warnings"].append("prospective_common_factor_concentration_exceeds_warning:evil")
    _write(proposal_path, proposal)
    _write(
        review_path,
        _review(
            workspace=workspace,
            findings_path=findings_path,
            proposal_path=proposal_path,
            selected_ticker="2331",
        ),
    )

    with pytest.raises(ReportError, match="concentration warnings do not match thresholds"):
        render(**paths)  # type: ignore[arg-type]


def test_render_shows_incomplete_common_factor_coverage_as_lower_bound(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    workspace = paths["workspace"]
    findings_path = paths["findings_path"]
    review_path = paths["review_path"]
    proposal_path = paths["proposal_path"]
    assert isinstance(workspace, Path)
    assert isinstance(findings_path, Path)
    assert isinstance(review_path, Path)
    assert isinstance(proposal_path, Path)
    proposal = yaml.safe_load(proposal_path.read_text(encoding="utf-8"))
    proposal["portfolio_exposure"]["common_factor_empty_tickers"] = ["1111", "9999"]
    proposal["warnings"].append("portfolio_exposure_common_factor_coverage_incomplete")
    _write(proposal_path, proposal)
    _write(
        review_path,
        _review(
            workspace=workspace,
            findings_path=findings_path,
            proposal_path=proposal_path,
            selected_ticker="2331",
        ),
    )

    document = render(**paths)  # type: ignore[arg-type]

    assert "common-factor tagなし" in document
    assert "1111, 9999" in document
    assert "declared tagsベースの下限値" in document


@pytest.mark.parametrize(
    ("empty_tickers", "add_warning", "message"),
    [
        (["9999"], False, "coverage warning does not match empty tickers"),
        ([], True, "coverage warning does not match empty tickers"),
        (["9999", "1111"], True, "must be sorted and unique"),
        (["9999", "9999"], True, "must be sorted and unique"),
        (["bad"], True, "contain an invalid ticker"),
    ],
)
def test_render_rejects_invalid_common_factor_coverage_with_fresh_review(
    tmp_path: Path,
    empty_tickers: list[str],
    add_warning: bool,
    message: str,
) -> None:
    paths = _workspace(tmp_path)
    workspace = paths["workspace"]
    findings_path = paths["findings_path"]
    review_path = paths["review_path"]
    proposal_path = paths["proposal_path"]
    assert isinstance(workspace, Path)
    assert isinstance(findings_path, Path)
    assert isinstance(review_path, Path)
    assert isinstance(proposal_path, Path)
    proposal = yaml.safe_load(proposal_path.read_text(encoding="utf-8"))
    proposal["portfolio_exposure"]["common_factor_empty_tickers"] = empty_tickers
    if add_warning:
        proposal["warnings"].append("portfolio_exposure_common_factor_coverage_incomplete")
    _write(proposal_path, proposal)
    _write(
        review_path,
        _review(
            workspace=workspace,
            findings_path=findings_path,
            proposal_path=proposal_path,
            selected_ticker="2331",
        ),
    )

    with pytest.raises(ReportError, match=message):
        render(**paths)  # type: ignore[arg-type]


def test_render_rejects_self_consistent_handwritten_order_numbers(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    workspace = paths["workspace"]
    findings_path = paths["findings_path"]
    review_path = paths["review_path"]
    proposal_path = paths["proposal_path"]
    assert isinstance(workspace, Path)
    assert isinstance(findings_path, Path)
    assert isinstance(review_path, Path)
    assert isinstance(proposal_path, Path)
    proposal = yaml.safe_load(proposal_path.read_text(encoding="utf-8"))
    proposal.update({"board_lot": 1, "close_yen": 1, "limit_price_yen": 1, "quantity": 300_000})
    proposal["notional_yen"] = 300_000
    _write(proposal_path, proposal)
    _write(
        review_path,
        _review(
            workspace=workspace,
            findings_path=findings_path,
            proposal_path=proposal_path,
            selected_ticker="2331",
        ),
    )

    with pytest.raises(ReportError, match="board_lot does not match"):
        render(**paths)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("max_acceptable_price_yen", 1_110, "max acceptable price does not match"),
        ("close_yen", 1_000, "close does not match"),
        ("limit_price_yen", 1_000, "price must equal the raw close"),
        ("quantity", 100, "quantity does not match plan-limit sizing"),
    ],
)
def test_render_rederives_each_plan_limit_number(
    tmp_path: Path, field: str, value: int, message: str
) -> None:
    paths = _workspace(tmp_path)
    workspace = paths["workspace"]
    findings_path = paths["findings_path"]
    review_path = paths["review_path"]
    proposal_path = paths["proposal_path"]
    assert isinstance(workspace, Path)
    assert isinstance(findings_path, Path)
    assert isinstance(review_path, Path)
    assert isinstance(proposal_path, Path)
    proposal = yaml.safe_load(proposal_path.read_text(encoding="utf-8"))
    proposal[field] = value
    _write(proposal_path, proposal)
    _write(
        review_path,
        _review(
            workspace=workspace,
            findings_path=findings_path,
            proposal_path=proposal_path,
            selected_ticker="2331",
        ),
    )

    with pytest.raises(ReportError, match=message):
        render(**paths)  # type: ignore[arg-type]


def test_render_rejects_stringified_order_numbers(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    workspace = paths["workspace"]
    findings_path = paths["findings_path"]
    review_path = paths["review_path"]
    proposal_path = paths["proposal_path"]
    assert isinstance(workspace, Path)
    assert isinstance(findings_path, Path)
    assert isinstance(review_path, Path)
    assert isinstance(proposal_path, Path)
    proposal = yaml.safe_load(proposal_path.read_text(encoding="utf-8"))
    proposal["quantity"] = "200"
    _write(proposal_path, proposal)
    _write(
        review_path,
        _review(
            workspace=workspace,
            findings_path=findings_path,
            proposal_path=proposal_path,
            selected_ticker="2331",
        ),
    )

    with pytest.raises(ReportError, match="proposal quantity must be a finite number"):
        render(**paths)  # type: ignore[arg-type]


def test_render_rejects_unknown_source_id_before_html_generation(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    findings_path = paths["findings_path"]
    assert isinstance(findings_path, Path)
    findings = yaml.safe_load(findings_path.read_text(encoding="utf-8"))
    findings["candidates"][0]["business_model"]["source_ids"] = ["invented-source"]
    _write(findings_path, findings)

    with pytest.raises(ReportError, match=r"unknown source_ids.*invented-source"):
        render(**paths)  # type: ignore[arg-type]


def test_render_rejects_selected_ticker_without_select_disposition(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    workspace = paths["workspace"]
    findings_path = paths["findings_path"]
    review_path = paths["review_path"]
    proposal_path = paths["proposal_path"]
    assert isinstance(workspace, Path)
    assert isinstance(findings_path, Path)
    assert isinstance(review_path, Path)
    assert isinstance(proposal_path, Path)
    comparison_path = workspace / "research-comparison.yaml"
    comparison = yaml.safe_load(comparison_path.read_text(encoding="utf-8"))
    comparison["candidates"][0]["disposition"] = "defer"
    _write(comparison_path, comparison)
    _write(
        review_path,
        _review(
            workspace=workspace,
            findings_path=findings_path,
            proposal_path=proposal_path,
            selected_ticker="2331",
        ),
    )

    with pytest.raises(ReportError, match="dispositions do not match"):
        render(**paths)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("five_year_base_cagr_pct", 99.99, "CAGR does not match packet"),
        ("fair_value_yen", 1, "fair value does not match packet"),
        ("fv_gap_pct", -99.0, "FV gap does not match packet"),
    ],
)
def test_render_rejects_comparison_numbers_that_disagree_with_packet(
    tmp_path: Path, field: str, value: float | int, message: str
) -> None:
    paths = _workspace(tmp_path)
    workspace = paths["workspace"]
    findings_path = paths["findings_path"]
    review_path = paths["review_path"]
    proposal_path = paths["proposal_path"]
    assert isinstance(workspace, Path)
    assert isinstance(findings_path, Path)
    assert isinstance(review_path, Path)
    assert isinstance(proposal_path, Path)
    comparison_path = workspace / "research-comparison.yaml"
    comparison = yaml.safe_load(comparison_path.read_text(encoding="utf-8"))
    comparison["candidates"][0][field] = value
    _write(comparison_path, comparison)
    _write(
        review_path,
        _review(
            workspace=workspace,
            findings_path=findings_path,
            proposal_path=proposal_path,
            selected_ticker="2331",
        ),
    )

    with pytest.raises(ReportError, match=message):
        render(**paths)  # type: ignore[arg-type]


def test_render_escapes_untrusted_findings_text(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    findings_path = paths["findings_path"]
    review_path = paths["review_path"]
    proposal_path = paths["proposal_path"]
    workspace = paths["workspace"]
    assert isinstance(findings_path, Path)
    assert isinstance(review_path, Path)
    assert isinstance(proposal_path, Path)
    assert isinstance(workspace, Path)
    findings = yaml.safe_load(findings_path.read_text(encoding="utf-8"))
    findings["candidates"][0]["assigned_questions"][0]["conclusion"] = (
        '<script>alert("owned")</script>'
    )
    _write(findings_path, findings)
    _write(
        review_path,
        _review(
            workspace=workspace,
            findings_path=findings_path,
            proposal_path=proposal_path,
            selected_ticker="2331",
        ),
    )

    document = render(**paths)  # type: ignore[arg-type]

    assert '<script>alert("owned")</script>' not in document
    assert "&lt;script&gt;alert(&quot;owned&quot;)&lt;/script&gt;" in document
    assert "TSE%3A2331" in document
    assert "script-src 'none'" in document


def test_render_shows_and_escapes_non_ok_source_decision_impact(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    findings_path = paths["findings_path"]
    review_path = paths["review_path"]
    proposal_path = paths["proposal_path"]
    workspace = paths["workspace"]
    assert isinstance(findings_path, Path)
    assert isinstance(review_path, Path)
    assert isinstance(proposal_path, Path)
    assert isinstance(workspace, Path)
    findings = yaml.safe_load(findings_path.read_text(encoding="utf-8"))
    metadata = findings["candidates"][0]["source_metadata"][0]
    metadata["status"] = "stale"
    metadata["note"] = "顧客集中を<b>確認不能</b>"
    _write(findings_path, findings)
    _write(
        review_path,
        _review(
            workspace=workspace,
            findings_path=findings_path,
            proposal_path=proposal_path,
            selected_ticker="2331",
        ),
    )

    document = render(**paths)  # type: ignore[arg-type]

    assert "decision impact" in document
    assert "顧客集中を&lt;b&gt;確認不能&lt;/b&gt;" in document
    assert "顧客集中を<b>確認不能</b>" not in document


def test_render_rejects_observed_evidence_from_unavailable_source(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    findings_path = paths["findings_path"]
    assert isinstance(findings_path, Path)
    findings = yaml.safe_load(findings_path.read_text(encoding="utf-8"))
    metadata = findings["candidates"][0]["source_metadata"][0]
    metadata["status"] = "failed"
    metadata["note"] = "一次資料を取得できず事実確認不能"
    _write(findings_path, findings)

    with pytest.raises(ReportError, match="observed evidence uses unavailable sources"):
        render(**paths)  # type: ignore[arg-type]


def test_render_shows_five_year_base_break_even_summary(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)

    document = render(**paths)  # type: ignore[arg-type]

    assert "要求5年CAGR" in document
    assert "8.00%" in document
    assert "base 1.10倍 /" in document
    assert "break-even 1.01倍 /" in document
    assert "downside buffer 0.09倍" in document
    assert "base 5.0% /" in document
    assert "break-even 3.3% /" in document
    assert "buffer 1.7pt" in document
    assert "観測trailing multiple" in document
    assert "10.32倍" in document
    assert "モデル範囲内" in document
    assert "観測値あり" in document
    assert (
        "buffer正 = 他の仮定を固定したとき要求CAGRを守りながら吸収できる悪化余地。"
        "負でも計算としては有効で、買い提案との整合はreview済み。" in document
    )


def test_render_scenario_table_shows_terminal_multiple_and_dividend_columns(
    tmp_path: Path,
) -> None:
    paths = _workspace(tmp_path)

    document = render(**paths)  # type: ignore[arg-type]

    assert "<th>terminal multiple</th>" in document
    assert "<th>累積配当</th>" in document
    assert "0.80倍" in document
    assert "150円" in document


def test_render_requires_entry_timing_when_a_proposal_exists(tmp_path: Path) -> None:
    paths = _workspace(tmp_path, entry_timing=None)

    with pytest.raises(ReportError, match="entry_timing"):
        render(**paths)  # type: ignore[arg-type]


def test_render_requires_entry_timing_even_for_a_defer_proposal(tmp_path: Path) -> None:
    paths = _workspace(tmp_path, mode="defer", entry_timing=None)

    with pytest.raises(ReportError, match="entry_timing"):
        render(**paths)  # type: ignore[arg-type]


def test_render_shows_entry_timing_and_nearest_dated_catalyst_in_purchase_method(
    tmp_path: Path,
) -> None:
    paths = _workspace(tmp_path)

    document = render(**paths)  # type: ignore[arg-type]

    assert "entry timing" in document
    assert _DEFAULT_ENTRY_TIMING in document
    assert "直近の確認event" in document
    assert "2026-08-05 FY2026通期決算" in document


def test_render_allows_missing_entry_timing_without_a_proposal(tmp_path: Path) -> None:
    paths = _workspace(tmp_path, mode="no_selected", entry_timing=None)

    document = render(**paths)  # type: ignore[arg-type]

    assert "no actionable bargain" in document


def test_external_links_are_https_only() -> None:
    assert _external_link("https://example.com/results.pdf") == "https://example.com/results.pdf"
    assert _external_link("http://example.com/results.pdf") is None
    assert _external_link("javascript:alert(1)") is None
    assert _external_link("https://user@example.com/results.pdf") is None
    assert _external_link("https://localhost/results.pdf") is None
    assert _external_link("https://127.0.0.1/results.pdf") is None
    assert _external_link("https://169.254.1.2/results.pdf") is None
    assert _external_link("https://2130706433/results.pdf") is None
    assert _external_link("https://017700000001/results.pdf") is None
    assert _external_link("https://0x7f000001/results.pdf") is None
    assert _external_link("https://127.1/results.pdf") is None
