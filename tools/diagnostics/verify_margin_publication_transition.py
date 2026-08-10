"""Verify the first all-issues daily margin snapshot against the final weekly one.

This is a one-shot U4 diagnostic for issue #892.  It reads a current market store,
checks the source/shape contract, applies predeclared universe and unit-continuity
gates, and writes the dated Markdown report that records the first observation.
It does not activate ingestion or change any runtime store.
"""

from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TextIO
from zoneinfo import ZoneInfo

MIN_SCHEMA_VERSION = 21
LEGACY_BALANCE_DATE = date(2026, 9, 18)
DAILY_BALANCE_DATE = date(2026, 9, 25)
LEGACY_PUBLICATION_DATE = date(2026, 9, 24)
JST = ZoneInfo("Asia/Tokyo")
DAILY_PUBLICATION_NOT_BEFORE = datetime(2026, 9, 28, 16, 0, tzinfo=JST)

# These gates are frozen before the first daily payload is observable.  They are
# intentionally wide: their purpose is to catch a different population, a unit
# change, or swapped fields, not to classify an ordinary economic move.
ROW_COUNT_RATIO_RANGE = (0.98, 1.02)
OVERLAP_COEFFICIENT_MIN = 0.98
PRIMARY_TOTAL_RATIO_RANGE = (0.50, 2.00)
ISSUE_TYPE_AGREEMENT_MIN = 0.95

BALANCE_FIELDS = (
    "long_vol",
    "short_vol",
    "long_std_vol",
    "long_neg_vol",
    "short_std_vol",
    "short_neg_vol",
)
PRIMARY_BALANCE_FIELDS = ("long_vol", "short_vol")


class MarginTransitionVerificationError(ValueError):
    """The store cannot support a truthful transition comparison."""


@dataclass(frozen=True)
class BalanceRow:
    ticker: str
    balances: tuple[float, float, float, float, float, float]
    issue_type: str


@dataclass(frozen=True)
class CoverageEvidence:
    source: str
    coverage_key: str
    coverage_start: str
    coverage_end: str
    fetched_at_utc: str
    record_count: int


@dataclass(frozen=True)
class Snapshot:
    balance_date: date
    rows: dict[str, BalanceRow]
    coverage: CoverageEvidence


@dataclass(frozen=True)
class FieldComparison:
    field: str
    legacy_total: float
    daily_total: float
    total_ratio: float | None
    comparable_ticker_count: int
    ratio_q10: float | None
    ratio_median: float | None
    ratio_q90: float | None
    zero_to_positive_count: int
    positive_to_zero_count: int


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    observed: str
    requirement: str


@dataclass(frozen=True)
class TransitionVerification:
    observed_at_utc: datetime
    sqlite_path: Path
    sqlite_size_bytes: int
    schema_version: int
    legacy: Snapshot
    daily: Snapshot
    overlap_tickers: tuple[str, ...]
    added_tickers: tuple[str, ...]
    removed_tickers: tuple[str, ...]
    row_count_ratio: float
    overlap_coefficient: float
    issue_type_agreement: float
    fields: tuple[FieldComparison, ...]
    gates: tuple[GateResult, ...]

    @property
    def passed(self) -> bool:
        return all(gate.passed for gate in self.gates)


def _require_finite_nonnegative(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MarginTransitionVerificationError(f"{label} is not numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0.0:
        raise MarginTransitionVerificationError(f"{label} is not finite and non-negative")
    return parsed


def _coverage_key(balance_date: date) -> str:
    iso = balance_date.isoformat()
    return f"get_mkt_margin_interest:{iso}..{iso}"


def _aware_datetime(value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise MarginTransitionVerificationError(f"{label} is not ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MarginTransitionVerificationError(f"{label} has no UTC offset")
    return parsed


def _load_snapshot(
    conn: sqlite3.Connection,
    *,
    table: str,
    date_column: str,
    source: str,
    balance_date: date,
    publication_not_before: date | datetime,
) -> Snapshot:
    iso = balance_date.isoformat()
    key = _coverage_key(balance_date)
    coverage_row = conn.execute(
        "SELECT coverage_start, coverage_end, fetched_at_utc, record_count, status, error "
        "FROM source_coverage WHERE source = ? AND coverage_key = ?",
        (source, key),
    ).fetchone()
    if coverage_row is None:
        raise MarginTransitionVerificationError(f"{source} has no exact coverage for {iso}")
    coverage_start, coverage_end, fetched_at, record_count, status, error = coverage_row
    if (
        coverage_start != iso
        or coverage_end != iso
        or status != "ok"
        or error is not None
        or not isinstance(fetched_at, str)
        or not fetched_at
    ):
        raise MarginTransitionVerificationError(
            f"{source} coverage for {iso} is not an exact clean snapshot"
        )
    fetched_datetime = _aware_datetime(fetched_at, label=f"{source} coverage fetched_at_utc")
    fetched_jst = fetched_datetime.astimezone(JST)
    if isinstance(publication_not_before, datetime):
        if fetched_jst < publication_not_before:
            raise MarginTransitionVerificationError(
                f"{source} coverage predates its {publication_not_before.isoformat()} publication"
            )
    elif fetched_jst.date() < publication_not_before:
        raise MarginTransitionVerificationError(
            f"{source} coverage predates its {publication_not_before.isoformat()} publication"
        )

    selected = ", ".join(("ticker", *BALANCE_FIELDS, "issue_type"))
    raw_rows = conn.execute(
        f"SELECT {selected} FROM {table} WHERE {date_column} = ? ORDER BY ticker",  # nosec B608
        (iso,),
    ).fetchall()
    if not raw_rows:
        raise MarginTransitionVerificationError(f"{source} snapshot for {iso} is empty")
    if not isinstance(record_count, int) or record_count != len(raw_rows):
        raise MarginTransitionVerificationError(
            f"{source} coverage count {record_count!r} differs from table count {len(raw_rows)}"
        )

    rows: dict[str, BalanceRow] = {}
    for raw in raw_rows:
        ticker = raw[0]
        if not isinstance(ticker, str) or not ticker or ticker in rows:
            raise MarginTransitionVerificationError(f"{source} has an invalid ticker identity")
        balances = tuple(
            _require_finite_nonnegative(value, label=f"{source}:{ticker}:{field}")
            for field, value in zip(BALANCE_FIELDS, raw[1:7], strict=True)
        )
        issue_type = raw[7]
        if not isinstance(issue_type, str) or not issue_type.strip():
            raise MarginTransitionVerificationError(f"{source}:{ticker}:issue_type is missing")
        long_total, short_total, long_std, long_neg, short_std, short_neg = balances
        if not math.isclose(long_total, long_std + long_neg, rel_tol=1e-12, abs_tol=1e-6):
            raise MarginTransitionVerificationError(
                f"{source}:{ticker}:long components do not sum to total"
            )
        if not math.isclose(short_total, short_std + short_neg, rel_tol=1e-12, abs_tol=1e-6):
            raise MarginTransitionVerificationError(
                f"{source}:{ticker}:short components do not sum to total"
            )
        rows[ticker] = BalanceRow(
            ticker=ticker,
            balances=(
                balances[0],
                balances[1],
                balances[2],
                balances[3],
                balances[4],
                balances[5],
            ),
            issue_type=issue_type,
        )

    return Snapshot(
        balance_date=balance_date,
        rows=rows,
        coverage=CoverageEvidence(
            source=source,
            coverage_key=key,
            coverage_start=iso,
            coverage_end=iso,
            fetched_at_utc=fetched_at,
            record_count=record_count,
        ),
    )


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _field_comparison(
    legacy: Snapshot,
    daily: Snapshot,
    overlap: tuple[str, ...],
    field_index: int,
) -> FieldComparison:
    legacy_total = sum(row.balances[field_index] for row in legacy.rows.values())
    daily_total = sum(row.balances[field_index] for row in daily.rows.values())
    ratios: list[float] = []
    zero_to_positive = 0
    positive_to_zero = 0
    for ticker in overlap:
        earlier = legacy.rows[ticker].balances[field_index]
        later = daily.rows[ticker].balances[field_index]
        if earlier > 0.0:
            ratios.append(later / earlier)
            positive_to_zero += later == 0.0
        else:
            zero_to_positive += later > 0.0
    return FieldComparison(
        field=BALANCE_FIELDS[field_index],
        legacy_total=legacy_total,
        daily_total=daily_total,
        total_ratio=(daily_total / legacy_total if legacy_total > 0.0 else None),
        comparable_ticker_count=len(ratios),
        ratio_q10=_quantile(ratios, 0.10),
        ratio_median=_quantile(ratios, 0.50),
        ratio_q90=_quantile(ratios, 0.90),
        zero_to_positive_count=zero_to_positive,
        positive_to_zero_count=positive_to_zero,
    )


def _primary_total_gate(comparison: FieldComparison) -> GateResult:
    ratio = comparison.total_ratio
    return GateResult(
        name=f"{comparison.field}_total_ratio",
        passed=(
            ratio is not None
            and PRIMARY_TOTAL_RATIO_RANGE[0] <= ratio <= PRIMARY_TOTAL_RATIO_RANGE[1]
        ),
        observed=f"{ratio:.6f}" if ratio is not None else "null",
        requirement=(f"{PRIMARY_TOTAL_RATIO_RANGE[0]:.2f}..{PRIMARY_TOTAL_RATIO_RANGE[1]:.2f}"),
    )


def verify_transition(
    sqlite_path: Path,
    *,
    observed_at_utc: datetime | None = None,
) -> TransitionVerification:
    """Read and compare the two boundary snapshots without mutating the store."""
    if not sqlite_path.is_file():
        raise MarginTransitionVerificationError(f"market store does not exist: {sqlite_path}")
    observed = observed_at_utc or datetime.now(UTC)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise MarginTransitionVerificationError("observed_at_utc must be timezone-aware")
    resolved = sqlite_path.resolve()
    report_path = sqlite_path if not sqlite_path.is_absolute() else Path(sqlite_path.name)
    stat_before = resolved.stat()
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version < MIN_SCHEMA_VERSION:
            raise MarginTransitionVerificationError(
                f"market schema version must be at least {MIN_SCHEMA_VERSION}; got {version}"
            )
        data_version_before = int(conn.execute("PRAGMA data_version").fetchone()[0])
        legacy = _load_snapshot(
            conn,
            table="jquants_weekly_margin",
            date_column="week_end",
            source="jquants_weekly_margin",
            balance_date=LEGACY_BALANCE_DATE,
            publication_not_before=LEGACY_PUBLICATION_DATE,
        )
        daily = _load_snapshot(
            conn,
            table="jquants_all_issues_daily_margin",
            date_column="balance_date",
            source="jquants_all_issues_daily_margin",
            balance_date=DAILY_BALANCE_DATE,
            publication_not_before=DAILY_PUBLICATION_NOT_BEFORE,
        )
        data_version_after = int(conn.execute("PRAGMA data_version").fetchone()[0])
        if data_version_before != data_version_after:
            raise MarginTransitionVerificationError("market store changed during verification")
    except sqlite3.Error as error:
        raise MarginTransitionVerificationError(f"cannot verify market store: {error}") from error
    finally:
        conn.close()
    stat_after = resolved.stat()
    if (stat_before.st_size, stat_before.st_mtime_ns) != (
        stat_after.st_size,
        stat_after.st_mtime_ns,
    ):
        raise MarginTransitionVerificationError("market store file changed during verification")
    for coverage in (legacy.coverage, daily.coverage):
        fetched_at = _aware_datetime(
            coverage.fetched_at_utc,
            label=f"{coverage.source} coverage fetched_at_utc",
        )
        if observed < fetched_at:
            raise MarginTransitionVerificationError(
                f"observation time predates {coverage.source} coverage"
            )

    legacy_tickers = set(legacy.rows)
    daily_tickers = set(daily.rows)
    overlap = tuple(sorted(legacy_tickers & daily_tickers))
    added = tuple(sorted(daily_tickers - legacy_tickers))
    removed = tuple(sorted(legacy_tickers - daily_tickers))
    row_count_ratio = len(daily.rows) / len(legacy.rows)
    overlap_coefficient = len(overlap) / min(len(legacy.rows), len(daily.rows))
    issue_type_agreement = (
        sum(legacy.rows[ticker].issue_type == daily.rows[ticker].issue_type for ticker in overlap)
        / len(overlap)
        if overlap
        else 0.0
    )
    fields = tuple(
        _field_comparison(legacy, daily, overlap, index) for index in range(len(BALANCE_FIELDS))
    )
    by_field = {field.field: field for field in fields}
    gates = (
        GateResult(
            name="row_count_ratio",
            passed=ROW_COUNT_RATIO_RANGE[0] <= row_count_ratio <= ROW_COUNT_RATIO_RANGE[1],
            observed=f"{row_count_ratio:.6f}",
            requirement=f"{ROW_COUNT_RATIO_RANGE[0]:.2f}..{ROW_COUNT_RATIO_RANGE[1]:.2f}",
        ),
        GateResult(
            name="ticker_overlap_coefficient",
            passed=overlap_coefficient >= OVERLAP_COEFFICIENT_MIN,
            observed=f"{overlap_coefficient:.6f}",
            requirement=f">={OVERLAP_COEFFICIENT_MIN:.2f}",
        ),
        GateResult(
            name="issue_type_agreement",
            passed=issue_type_agreement >= ISSUE_TYPE_AGREEMENT_MIN,
            observed=f"{issue_type_agreement:.6f}",
            requirement=f">={ISSUE_TYPE_AGREEMENT_MIN:.2f}",
        ),
        *(_primary_total_gate(by_field[field]) for field in PRIMARY_BALANCE_FIELDS),
    )
    return TransitionVerification(
        observed_at_utc=observed.astimezone(UTC),
        sqlite_path=report_path,
        sqlite_size_bytes=stat_after.st_size,
        schema_version=version,
        legacy=legacy,
        daily=daily,
        overlap_tickers=overlap,
        added_tickers=added,
        removed_tickers=removed,
        row_count_ratio=row_count_ratio,
        overlap_coefficient=overlap_coefficient,
        issue_type_agreement=issue_type_agreement,
        fields=fields,
        gates=gates,
    )


def _format_number(value: float | None) -> str:
    return "—" if value is None else f"{value:,.6f}"


def _ticker_list(values: tuple[str, ...], *, limit: int = 50) -> str:
    if not values:
        return "なし"
    shown = ", ".join(f"`{ticker}`" for ticker in values[:limit])
    if len(values) > limit:
        return f"{shown}（ほか {len(values) - limit} 銘柄）"
    return shown


def render_report(result: TransitionVerification) -> str:
    """Render the immutable human-facing evidence report."""
    verdict = "pass" if result.passed else "fail"
    conclusion = (
        "新旧 snapshot は事前固定した母集団・単位連続性 gate を通過した。"
        "これは同じ物理量の取込継続を確認するもので、日次軸の投資有効性は評価しない。"
        if result.passed
        else (
            "母集団または単位連続性 gate が不一致である。"
            "activation と merge を停止して原因を確認する。"
        )
    )
    lines = [
        f"# 信用取引残高 公表移行初回検証（{result.daily.balance_date.isoformat()} 残高）",
        "",
        "価値tier: T1 — 公表制度変更を欠損・語義汚染なく通過する",
        "",
        "## 判定",
        "",
        f"- machine verdict: `{verdict}`",
        f"- observed_at_utc: `{result.observed_at_utc.isoformat()}`",
        f"- market store: `{result.sqlite_path}`（{result.sqlite_size_bytes:,} bytes）",
        f"- market schema version: `{result.schema_version}`",
        f"- 結論: {conclusion}",
        "",
        "## Source snapshot",
        "",
        "| series | balance date | rows | coverage fetched_at_utc | coverage key |",
        "| --- | --- | ---: | --- | --- |",
        (
            f"| legacy weekly | {result.legacy.balance_date.isoformat()} | "
            f"{len(result.legacy.rows):,} | {result.legacy.coverage.fetched_at_utc} | "
            f"`{result.legacy.coverage.coverage_key}` |"
        ),
        (
            f"| all-issues daily | {result.daily.balance_date.isoformat()} | "
            f"{len(result.daily.rows):,} | {result.daily.coverage.fetched_at_utc} | "
            f"`{result.daily.coverage.coverage_key}` |"
        ),
        "",
        "## 事前固定 gate",
        "",
        "| gate | observed | requirement | result |",
        "| --- | ---: | ---: | --- |",
    ]
    lines.extend(
        f"| `{gate.name}` | {gate.observed} | {gate.requirement} | "
        f"{'pass' if gate.passed else 'fail'} |"
        for gate in result.gates
    )
    lines.extend(
        [
            "",
            "## 母集団",
            "",
            f"- overlap: {len(result.overlap_tickers):,}",
            f"- added: {len(result.added_tickers):,} — {_ticker_list(result.added_tickers)}",
            f"- removed: {len(result.removed_tickers):,} — {_ticker_list(result.removed_tickers)}",
            "",
            "## 残高規模と銘柄別変化分布",
            "",
            (
                "銘柄別比率は旧値が正の overlap 銘柄だけで計算する。total ratio の gate は "
                "`long_vol` / `short_vol` のみに適用し、内訳は診断として記録する。"
            ),
            "",
            (
                "| field | legacy total | daily total | total ratio | comparable | "
                "ticker ratio q10 | median | q90 | 0→positive | positive→0 |"
            ),
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    lines.extend(
        (
            f"| `{field.field}` | {field.legacy_total:,.0f} | {field.daily_total:,.0f} | "
            f"{_format_number(field.total_ratio)} | {field.comparable_ticker_count:,} | "
            f"{_format_number(field.ratio_q10)} | {_format_number(field.ratio_median)} | "
            f"{_format_number(field.ratio_q90)} | {field.zero_to_positive_count:,} | "
            f"{field.positive_to_zero_count:,} |"
        )
        for field in result.fields
    )
    lines.extend(
        [
            "",
            "## 解釈境界",
            "",
            (
                "この検証は source coverage、件数、ticker 母集団、issue type、"
                "残高単位と内訳恒等式を確認する。"
            ),
            "日次系列を既存 `margin_*`、gate、rank、E[r] へ接続する根拠にはしない。",
            "",
        ]
    )
    return "\n".join(lines)


def _parse_observed_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO-8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("must include a UTC offset")
    return parsed.astimezone(UTC)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, required=True, help="current market.sqlite")
    parser.add_argument("--output", type=Path, required=True, help="dated Markdown report path")
    parser.add_argument(
        "--observed-at",
        type=_parse_observed_at,
        help="ISO-8601 observation timestamp; defaults to current UTC",
    )
    return parser


def _validate_output_path(sqlite_path: Path, output_path: Path) -> None:
    sqlite_resolved = sqlite_path.resolve()
    output_resolved = output_path.resolve()
    if sqlite_resolved == output_resolved:
        raise MarginTransitionVerificationError("report output must not be the market store")
    if output_path.exists() and output_path.samefile(sqlite_path):
        raise MarginTransitionVerificationError("report output must not alias the market store")


def _write_report_atomic(output_path: Path, report: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_name = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(report)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(output_path)
    finally:
        temp_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None, *, stderr: TextIO = sys.stderr) -> int:
    args = build_parser().parse_args(argv)
    try:
        _validate_output_path(args.sqlite, args.output)
        result = verify_transition(args.sqlite, observed_at_utc=args.observed_at)
    except MarginTransitionVerificationError as error:
        print(f"margin publication transition verification failed: {error}", file=stderr)
        return 2
    try:
        _write_report_atomic(args.output, render_report(result))
    except OSError as error:
        print(
            f"margin publication transition report write failed: {type(error).__name__}: {error}",
            file=stderr,
        )
        return 2
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
