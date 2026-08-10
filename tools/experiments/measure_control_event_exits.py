"""Compare the calibration store with and without realized tender-offer exit values.

Read-only. It reads two calibration stores built from the same panels — one whose
forward windows were resolved from market closes alone, one that also priced delisted
names with the cash a completed tender offer paid — plus the evaluation payload each
produced, and states what moved.

The comparison is fixed in advance in
`reports/studies/2026-08-11-capital-control-exit-values/preregistration.md`: which rows
may be replaced, what has to stay byte-identical, and which conclusions are read. This
script computes exactly that and nothing else, so the report cannot be assembled from a
different slice than the one registered.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sqlite3
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

import yaml

from baibai_engine.foundation.yaml_io import safe_load

CONTROL_EVENT_STATUS = "resolved_control_event_exit"
AUTHORITY_HORIZONS = ("3y", "5y")
CORE_METRICS = ("recommended_rank_top5", "recommended_rank_top10", "er_calibration")
# The fields a replaced row is allowed to change. Everything else has to match the
# baseline row exactly, which is what proves the replacement touched nothing but the
# exit itself.
# The only two statuses the pre-registration allows an offer price to replace. A row the
# market closed keeps its observed quote, so finding one replaced means the contract was
# violated, not that more rows resolved.
REPLACEABLE_PRIOR_STATUSES = frozenset({"unresolved_missing_exit", "unresolved_stale_exit"})
REPLACEABLE_FIELDS = frozenset(
    {
        "resolved",
        "price_return",
        "exit_date",
        "status",
        "stale_price",
        "realized_dividend_sum",
        "realized_dividend_fy_count",
        "total_return",
        "total_return_status",
    }
)


class ComparisonError(RuntimeError):
    """The two stores cannot be compared on the registered terms."""


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _key(row: Mapping[str, str]) -> tuple[str, str, str]:
    return row["asof"], row["ticker"], row["horizon"]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _panel_identity(root: Path) -> dict[str, str]:
    return {path.name: _sha256(path) for path in sorted(root.glob("panel-*.csv"))}


def compare_forward(baseline_dir: Path, actual_dir: Path) -> dict[str, Any]:
    """Count what the exit values replaced and prove nothing else moved."""
    baseline_files = {path.name for path in baseline_dir.glob("forward-*.csv")}
    actual_files = {path.name for path in actual_dir.glob("forward-*.csv")}
    if baseline_files != actual_files:
        raise ComparisonError("the two stores hold different cohorts")

    replaced_by_horizon: Counter[str] = Counter()
    replaced_prior_status: Counter[str] = Counter()
    replaced_tickers: set[str] = set()
    replaced_cohorts: dict[str, set[str]] = {}
    unresolved_before: Counter[str] = Counter()
    unresolved_after: Counter[str] = Counter()
    unexpected: list[str] = []
    for name in sorted(baseline_files):
        before = {_key(row): row for row in _rows(baseline_dir / name)}
        after = {_key(row): row for row in _rows(actual_dir / name)}
        if before.keys() != after.keys():
            raise ComparisonError(f"{name} holds a different row set in the two stores")
        for key, baseline_row in before.items():
            actual_row = after[key]
            horizon = key[2]
            if baseline_row["status"].startswith("unresolved"):
                unresolved_before[horizon] += 1
            if actual_row["status"].startswith("unresolved"):
                unresolved_after[horizon] += 1
            if baseline_row == actual_row:
                continue
            changed = {
                field for field in baseline_row if baseline_row[field] != actual_row.get(field)
            }
            if (
                actual_row["status"] != CONTROL_EVENT_STATUS
                or not changed <= REPLACEABLE_FIELDS
                or baseline_row["status"] not in REPLACEABLE_PRIOR_STATUSES
            ):
                unexpected.append(f"{name}:{key[1]}:{horizon}")
                continue
            replaced_by_horizon[horizon] += 1
            replaced_prior_status[baseline_row["status"]] += 1
            replaced_tickers.add(key[1])
            replaced_cohorts.setdefault(horizon, set()).add(key[0])
    return {
        "replaced_rows_by_horizon": dict(sorted(replaced_by_horizon.items())),
        "replaced_prior_status_counts": dict(sorted(replaced_prior_status.items())),
        "replaced_ticker_count": len(replaced_tickers),
        "replaced_cohort_count_by_horizon": {
            horizon: len(asofs) for horizon, asofs in sorted(replaced_cohorts.items())
        },
        "unresolved_rows_before_by_horizon": dict(sorted(unresolved_before.items())),
        "unresolved_rows_after_by_horizon": dict(sorted(unresolved_after.items())),
        "rows_changed_outside_the_replacement_rule": sorted(unexpected),
    }


def compare_authority(baseline: Mapping[str, Any], actual: Mapping[str, Any]) -> dict[str, Any]:
    """Read the eligibility and blocker counts each evaluation reported."""
    result: dict[str, Any] = {}
    for label, payload in (("baseline", baseline), ("actual_exit", actual)):
        coverage = payload.get("authority_coverage")
        coverage = coverage if isinstance(coverage, Mapping) else {}
        integrity = payload.get("cohort_integrity")
        integrity = integrity if isinstance(integrity, list) else []
        eligible = Counter[str]()
        blockers = Counter[str]()
        for item in integrity:
            if not isinstance(item, Mapping) or item.get("horizon") not in AUTHORITY_HORIZONS:
                continue
            horizon = str(item["horizon"])
            if item.get("integrity_status") == "eligible":
                eligible[horizon] += 1
            for reason in item.get("blocking_reasons") or ():
                blockers[f"{horizon}:{reason}"] += 1
        result[label] = {
            "eligible_cohorts_by_horizon": dict(sorted(eligible.items())),
            "blocking_reason_counts": dict(sorted(blockers.items())),
            "eligible_metric_cohort_count": coverage.get("eligible_metric_cohort_count"),
            "blocked_or_unresolved_count": coverage.get("blocked_or_unresolved_count"),
        }
    return result


def compare_core_metrics(baseline: Mapping[str, Any], actual: Mapping[str, Any]) -> dict[str, Any]:
    """Report each conclusion's aggregate value and sign on both contracts."""
    result: dict[str, Any] = {}
    for horizon in AUTHORITY_HORIZONS:
        result[horizon] = {
            metric: {
                "baseline": _metric_value(baseline, horizon, metric),
                "actual_exit": _metric_value(actual, horizon, metric),
            }
            for metric in CORE_METRICS
        }
    return result


def _metric_value(payload: Mapping[str, Any], horizon: str, metric: str) -> Any:
    results = payload.get("results")
    horizon_payload = results.get(horizon) if isinstance(results, Mapping) else None
    aggregate = horizon_payload.get("aggregate") if isinstance(horizon_payload, Mapping) else None
    if not isinstance(aggregate, Mapping):
        return None
    node = aggregate.get(metric)
    return node if not isinstance(node, Mapping) else dict(node)


def source_coverage(market_sqlite: Path) -> dict[str, Any]:
    """Count the primary-source rows the derivation actually had."""
    connection = sqlite3.connect(f"file:{market_sqlite}?mode=ro", uri=True)
    try:
        delistings = int(
            connection.execute("SELECT COUNT(*) FROM jpx_delistings").fetchone()[0] or 0
        )
        tender_offer_delistings = int(
            connection.execute(
                "SELECT COUNT(*) FROM jpx_delistings WHERE reason LIKE '%公開買付%'"
            ).fetchone()[0]
            or 0
        )
        exits = int(
            connection.execute("SELECT COUNT(*) FROM tender_offer_exit_values").fetchone()[0] or 0
        )
        by_form = {
            str(code): int(count)
            for code, count in connection.execute(
                "SELECT doc_type_code, COUNT(*) FROM edinet_documents "
                "WHERE doc_type_code IN ('240','250','260','270','280','350','360') "
                "GROUP BY doc_type_code ORDER BY doc_type_code"
            )
        }
        policy_months = int(
            connection.execute(
                "SELECT COUNT(DISTINCT snapshot_month_end) FROM tse_capital_policy_snapshots"
            ).fetchone()[0]
            or 0
        )
        policy_rows = int(
            connection.execute("SELECT COUNT(*) FROM tse_capital_policy_snapshots").fetchone()[0]
            or 0
        )
    finally:
        connection.close()
    return {
        "jpx_delisting_rows": delistings,
        "jpx_tender_offer_delisting_rows": tender_offer_delistings,
        "tender_offer_exit_values": exits,
        "edinet_rows_by_form": by_form,
        "tse_capital_policy_months": policy_months,
        "tse_capital_policy_rows": policy_rows,
    }


def build_measurement(
    *,
    baseline_dir: Path,
    actual_dir: Path,
    baseline_evaluation: Path,
    actual_evaluation: Path,
    market_sqlite: Path,
) -> dict[str, Any]:
    baseline_payload = safe_load(baseline_evaluation.read_text(encoding="utf-8"))
    actual_payload = safe_load(actual_evaluation.read_text(encoding="utf-8"))
    if not isinstance(baseline_payload, dict) or not isinstance(actual_payload, dict):
        raise ComparisonError("an evaluation payload is not a mapping")
    baseline_panels = _panel_identity(baseline_dir)
    actual_panels = _panel_identity(actual_dir)
    return {
        "kind": "control-event-exit-comparison",
        "preregistration": "reports/studies/2026-08-11-capital-control-exit-values/"
        "preregistration.md",
        "inputs": {
            "baseline_dir": str(baseline_dir),
            "actual_dir": str(actual_dir),
            "baseline_evaluation_sha256": _sha256(baseline_evaluation),
            "actual_evaluation_sha256": _sha256(actual_evaluation),
            "market_store_sha256": _sha256(market_sqlite),
            "screening_rules_hash": actual_payload.get("screening_rules_hash"),
            "cache_schema_version": actual_payload.get("cache_schema_version"),
            "er_model_version": actual_payload.get("er_model_version"),
        },
        "source_coverage": source_coverage(market_sqlite),
        "forward": compare_forward(baseline_dir, actual_dir),
        "authority": compare_authority(baseline_payload, actual_payload),
        "core_metrics": compare_core_metrics(baseline_payload, actual_payload),
        "integrity": {
            "panel_count": len(actual_panels),
            "panels_identical": baseline_panels == actual_panels,
        },
    }


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--actual-dir", type=Path, required=True)
    parser.add_argument("--baseline-evaluation", type=Path, required=True)
    parser.add_argument("--actual-evaluation", type=Path, required=True)
    parser.add_argument("--market-sqlite", type=Path, default=Path("stores/market/market.sqlite"))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        payload = build_measurement(
            baseline_dir=args.baseline_dir,
            actual_dir=args.actual_dir,
            baseline_evaluation=args.baseline_evaluation,
            actual_evaluation=args.actual_evaluation,
            market_sqlite=args.market_sqlite,
        )
    except (ComparisonError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    if args.out is None:
        print(text, file=stdout or sys.stdout, end="")
    else:
        args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
