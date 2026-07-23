"""Create a compact, hash-bound review draft before rendering the large HTML projection."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from baibai_engine.foundation.filesystem import write_text_atomic

from .render import (
    _REVIEW_CHECK_IDS,
    ReportError,
    _comparison_index,
    _dict_rows,
    _load_findings,
    _load_mapping,
    _load_thesis,
    _require_completed_comparison,
    _validate_comparison_values,
    _validate_proposal,
    _validate_sources,
    _verify_manifest_inputs,
    review_bindings,
)


def scaffold(
    *, workspace: Path, findings_path: Path, proposal_path: Path | None
) -> dict[str, object]:
    findings = _load_findings(findings_path)
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    _verify_manifest_inputs(manifest)
    if str(manifest.get("as_of")) != findings.meta.as_of.isoformat():
        raise ReportError("findings as_of does not match workspace manifest")
    selection = _load_mapping(workspace / "selection.yaml", label="workspace selection")
    comparison = _load_mapping(workspace / "research-comparison.yaml", label="research comparison")
    shortlist = _dict_rows(selection.get("shortlist"), label="selection shortlist")
    tickers = tuple(str(item.get("ticker")) for item in shortlist)
    if set(tickers) != {item.ticker for item in findings.candidates}:
        raise ReportError("findings tickers must exactly match selection.shortlist")
    selected = comparison.get("selected_ticker")
    selected_ticker = str(selected) if selected is not None else None
    rows = _comparison_index(comparison)
    findings_by = {item.ticker: item for item in findings.candidates}
    thesis_paths = {}
    theses = {}
    for ticker in tickers:
        if ticker not in rows:
            raise ReportError(f"comparison is missing shortlist ticker {ticker}")
        _require_completed_comparison(rows[ticker], ticker)
        thesis_path, thesis = _load_thesis(workspace, ticker)
        _validate_comparison_values(rows[ticker], thesis, ticker)
        _validate_sources(findings_by[ticker], thesis)
        thesis_paths[ticker] = thesis_path
        theses[ticker] = thesis
    if not isinstance(comparison.get("ranking_rationale"), str):
        raise ReportError("research comparison needs ranking_rationale")
    _validate_proposal(
        proposal_path=proposal_path,
        selected_ticker=selected_ticker,
        thesis_path=thesis_paths.get(selected_ticker) if selected_ticker else None,
        thesis=theses.get(selected_ticker) if selected_ticker else None,
        manifest=manifest,
        report_as_of=findings.meta.as_of,
        report_budget_yen=findings.meta.budget_yen,
        target_session=findings.meta.target_session,
    )
    return {
        "schema_version": 1,
        "reviewer_role": "independent_second_pass",
        "reviewer_identity": None,
        "reviewer_run_id": None,
        "reviewed_at": None,
        "conclusion": "pending",
        "reviewed_inputs": review_bindings(
            workspace=workspace,
            findings_path=findings_path,
            proposal_path=proposal_path,
            selected_ticker=selected_ticker,
            thesis_paths=thesis_paths,
            theses=theses,
        ),
        "checks": [
            {"check_id": check_id, "status": "pending", "note": None}
            for check_id in sorted(_REVIEW_CHECK_IDS)
        ],
        "findings": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scaffold a review bound to report inputs.")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--findings", required=True, type=Path)
    parser.add_argument("--proposal", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error(f"review draft already exists: {args.out}")
    try:
        document = scaffold(
            workspace=args.workspace, findings_path=args.findings, proposal_path=args.proposal
        )
    except ReportError as error:
        parser.error(str(error))
    write_text_atomic(
        args.out,
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True, default_flow_style=False),
    )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
