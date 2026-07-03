"""calibration-build / calibration-evaluate: 長期見積り較正の CLI 実装。

どちらも local SQLite / local store だけを読む (provider 呼び出しなし・
credentials 不要) 。build は panel + forward return を月次 asof ごとに永続化し、
evaluate は永続化済み cohort 群から評価 YAML を出力する。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import TextIO

import yaml

from ..rule_config import ScreeningRules
from .evaluation import TRAP_EXCESS_THRESHOLD, evaluate_cohorts
from .forward import HORIZONS, ForwardReturnRow, compute_forward_returns
from .grid import month_end_asof_grid
from .panel import CalibrationError, build_panel
from .store import (
    forward_path,
    panel_path,
    read_forward,
    read_panel,
    read_panel_meta,
    write_forward,
    write_panel,
)


def calibration_build_command(
    *,
    sqlite_path: Path,
    calibration_dir: Path,
    rules: ScreeningRules,
    start: date,
    end: date,
    force: bool = False,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    asofs = month_end_asof_grid(sqlite_path, start=start, end=end)
    if not asofs:
        print("no month-end trading days found in the requested window", file=sys.stderr)
        return 1
    print(
        f"calibration build: {len(asofs)} cohort(s) "
        f"{asofs[0].isoformat()}..{asofs[-1].isoformat()}",
        file=out,
        flush=True,
    )
    built = 0
    tickers: set[str] = set()
    for asof in asofs:
        path = panel_path(calibration_dir, asof)
        if path.exists() and not force:
            rows = read_panel(calibration_dir, asof)
            tickers.update(row.ticker for row in rows)
            print(f"calibration build: panel {asof.isoformat()} exists (skip)", file=out)
            continue
        try:
            result = build_panel(asof, sqlite_path=sqlite_path, rules=rules)
        except CalibrationError as exc:
            print(f"calibration build: {asof.isoformat()} failed: {exc}", file=sys.stderr)
            return 1
        write_panel(calibration_dir, asof, result.rows, result.diagnostics)
        tickers.update(row.ticker for row in result.rows)
        built += 1
        diag = result.diagnostics
        print(
            f"calibration build: panel {asof.isoformat()} "
            f"universe={diag.universe_size} population={diag.population_size} "
            f"candidates={diag.candidates} "
            f"per_trailing={diag.population_per_trailing_nonnull} "
            f"pbr={diag.population_pbr_nonnull}",
            file=out,
            flush=True,
        )

    print(
        f"calibration build: forward returns for {len(tickers)} ticker(s) "
        f"x {len(asofs)} cohort(s) start",
        file=out,
        flush=True,
    )
    forward_rows = compute_forward_returns(sqlite_path, asofs=asofs, tickers=sorted(tickers))
    rows_by_asof: dict[str, list[ForwardReturnRow]] = {}
    for row in forward_rows:
        rows_by_asof.setdefault(row.asof, []).append(row)
    for asof in asofs:
        write_forward(calibration_dir, asof, rows_by_asof.get(asof.isoformat(), []))
    resolved = sum(1 for row in forward_rows if row.resolved)
    print(
        f"calibration build: done (panels built={built}, forward rows={len(forward_rows)}, "
        f"resolved={resolved})",
        file=out,
        flush=True,
    )
    return 0


def calibration_evaluate_command(
    *,
    calibration_dir: Path,
    horizons: list[str] | None = None,
    output_path: Path | None = None,
    start: date | None = None,
    end: date | None = None,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    horizons = horizons or list(HORIZONS)
    unknown = [horizon for horizon in horizons if horizon not in HORIZONS]
    if unknown:
        print(f"unknown horizon(s): {', '.join(unknown)}", file=sys.stderr)
        return 1
    all_asofs = [
        date.fromisoformat(path.stem.removeprefix("panel-"))
        for path in calibration_dir.glob("panel-*.csv")
    ]
    asofs = sorted(
        asof
        for asof in all_asofs
        if (start is None or asof >= start) and (end is None or asof <= end)
    )
    if not asofs:
        print(f"no panels found under {calibration_dir}", file=sys.stderr)
        return 1
    consistency_error = _panel_consistency_error(calibration_dir, asofs)
    if consistency_error is not None:
        print(consistency_error, file=sys.stderr)
        return 1
    panels = {asof.isoformat(): read_panel(calibration_dir, asof) for asof in asofs}
    forwards = {
        asof.isoformat(): (
            read_forward(calibration_dir, asof)
            if forward_path(calibration_dir, asof).exists()
            else []
        )
        for asof in asofs
    }
    result = evaluate_cohorts(panels, forwards, horizons=horizons)
    payload = {
        "kind": "estimate-calibration-evaluation",
        "cohort_window": {
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
        },
        "cohort_asofs": [asof.isoformat() for asof in asofs],
        "horizons": horizons,
        "trap_excess_threshold": TRAP_EXCESS_THRESHOLD,
        "excess_basis": "population_median_total_return_dividend_accrual",
        "results": result,
    }
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, default_flow_style=False)
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
        print(f"calibration evaluate: wrote {output_path}", file=out)
    else:
        print(text, file=out)
    return 0


def _panel_consistency_error(calibration_dir: Path, asofs: list[date]) -> str | None:
    """store の cohort 群が単一の rules・重複のない月次 grid であることを検証する。

    rules 改訂後の再構築漏れ (新旧 rank の混在) と、同一月の重複 cohort
    (forward 窓が ~96% 重複し集計を二重計上する) を評価前に機械検出する。
    """
    hashes: dict[str, list[str]] = {}
    months: dict[tuple[int, int], list[str]] = {}
    for asof in asofs:
        meta = read_panel_meta(calibration_dir, asof)
        rules_hash = meta.get("rules_hash")
        key = str(rules_hash) if isinstance(rules_hash, str) and rules_hash else "(missing)"
        hashes.setdefault(key, []).append(asof.isoformat())
        months.setdefault((asof.year, asof.month), []).append(asof.isoformat())
    if len(hashes) > 1 or "(missing)" in hashes:
        summary = "; ".join(
            f"{key}: {values[0]}..{values[-1]} ({len(values)})"
            for key, values in sorted(hashes.items())
        )
        return (
            "panel store mixes rules provenance — rebuild with calibration-build --force. "
            f"rules_hash groups: {summary}"
        )
    duplicated = {month: values for month, values in months.items() if len(values) > 1}
    if duplicated:
        listing = "; ".join(
            f"{year}-{month:02d}: {', '.join(values)}"
            for (year, month), values in sorted(duplicated.items())
        )
        return f"panel store has duplicate cohorts in the same month — remove extras: {listing}"
    return None
