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
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    horizons = horizons or list(HORIZONS)
    unknown = [horizon for horizon in horizons if horizon not in HORIZONS]
    if unknown:
        print(f"unknown horizon(s): {', '.join(unknown)}", file=sys.stderr)
        return 1
    asofs = sorted(
        date.fromisoformat(path.stem.removeprefix("panel-"))
        for path in calibration_dir.glob("panel-*.csv")
    )
    if not asofs:
        print(f"no panels found under {calibration_dir}", file=sys.stderr)
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
        "cohort_asofs": [asof.isoformat() for asof in asofs],
        "horizons": horizons,
        "trap_excess_threshold": TRAP_EXCESS_THRESHOLD,
        "excess_basis": "population_median_total_return_or_price_return",
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
