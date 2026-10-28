"""Merge source coverage while retaining target-owned derived and origin tables.

The source may be dehydrated; its lake-owned facts are not copied by this merge.
General coverage rows are unioned with matching payloads except fetch timestamps.
Financial-summary counts are validated against hydrated target rows and may only
be raised. New clean/failed overlaps introduced by the union are rejected; existing
partial repairs retain the writer's coverage semantics.

Short-sale coverage follows the latest fetch, including an incomplete correction. Imported
EDINET document claims require recheck; target document claims remain authoritative.
Target-derived capital-policy snapshots and the lake-origin marker are not merged.
Both files must already satisfy the current schema; this is not a migration.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from baibai_engine.storage.observations.schema import SQLITE_SCHEMA_VERSION
from baibai_engine.storage.observations.schema import SQLiteSchemaError
from baibai_engine.storage.observations.schema import validate_current_schema
from baibai_jobs.storage.store_merge import (
    MergeError,
    MergeReport,
    RowFilter,
    TableMerge,
    count,
    merge_fact_tables,
    schema_name,
)

# The ledger of fetches, with the key that decides whether a row is the same row. It is
# the one accumulating table the lake does not own, so it is the one this merge unions.
FACT_KEYS: Mapping[str, tuple[str, ...]] = {
    "source_coverage": ("source", "coverage_key"),
}

# Store-local publication metadata belongs to the target SQLite generation. Importing
# the cloud copy's marker would claim that target rows came from a release they did not
# come from; hydrate/publish are the only writers allowed to advance it.
LOCAL_METADATA_KEYS: Mapping[str, tuple[str, ...]] = {
    "lake_store_origin": ("singleton",),
}
# Derived snapshots are lake datasets. Hydrate replaces those tables from the selected
# release, so a row removed by a later derivation is not reinstated by store merge.

TARGET_RETAINED_KEYS: Mapping[str, tuple[str, ...]] = LOCAL_METADATA_KEYS
ALL_TABLES: Mapping[str, tuple[str, ...]] = {**FACT_KEYS, **TARGET_RETAINED_KEYS}

# When a store read the source, not what the source said. Two stores that read the same
# range at different moments differ here by construction, so comparing fetch times
# would reject equivalent coverage. The insert keeps the target's timestamp.
UNCOMPARED: Mapping[str, tuple[str, ...]] = {
    "source_coverage": ("fetched_at_utc",),
}

# `record_count` receives a table-aware comparison below. It proves every clean
# financial-summary range against the corresponding rows before and after the union,
# while other sources and non-clean shared rows retain exact payload agreement.
_CUSTOM_COMPARED: Mapping[str, tuple[str, ...]] = {
    "source_coverage": ("record_count",),
}
_MERGE_EXEMPTIONS: Mapping[str, tuple[str, ...]] = {
    table: (*UNCOMPARED.get(table, ()), *_CUSTOM_COMPARED.get(table, ()))
    for table in FACT_KEYS
    if table in UNCOMPARED or table in _CUSTOM_COMPARED
}

_SHORT_SALE_REPORT_SOURCE = "jquants_short_sale_reports"
_UNION_COVERAGE = RowFilter(
    unaliased="source NOT IN ('jquants_short_sale_reports', 'edinet_research_facts')",
    aliased="s.source NOT IN ('jquants_short_sale_reports', 'edinet_research_facts')",
)


@dataclass(frozen=True, slots=True)
class _ShortCoverageClaim:
    fetched_at: datetime
    fetched_at_text: str
    record_count: int
    status: str
    error: str | None


def merge_stores(source: Path, target: Path) -> MergeReport:
    """Reconcile eligible coverage and keep target-owned tables in one transaction."""

    for path in (source, target):
        if not path.is_file():
            raise MergeError(f"market store does not exist: {path}")
    _require_schema(source)
    _require_schema(target)
    uri = target.resolve().as_uri()
    with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as connection:
        connection.execute("ATTACH DATABASE ? AS source", (f"{source.resolve().as_uri()}?mode=ro",))
        try:
            _require_attached_version(connection, path=source, schema="source")
            # No separate column comparison: the shape validator above pins every column
            # of every table on both stores, so two stores that reach here cannot differ.
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Short-sale snapshots and document extraction claims have separate
                # reconciliation rules. Count around all passes for the combined report.
                source_rows = count(connection, "SELECT count(*) FROM source.source_coverage")
                before = count(connection, "SELECT count(*) FROM main.source_coverage")
                known_overlaps = _coverage_overlaps(connection, schema="main") | _coverage_overlaps(
                    connection, schema="source"
                )
                _require_no_overclaimed_coverage(connection)
                _merge_short_sale_coverage(connection)
                _merge_edinet_fact_coverage(connection)
                merge_fact_tables(
                    connection,
                    {"source_coverage": FACT_KEYS["source_coverage"]},
                    eligible=_UNION_COVERAGE,
                    uncompared=_MERGE_EXEMPTIONS,
                )
                _raise_fin_summary_coverage_counts(connection)
                _require_no_reinstated_coverage(connection, known_overlaps=known_overlaps)
                retained = _retain_target_tables(connection)
                after = count(connection, "SELECT count(*) FROM main.source_coverage")
                connection.commit()
                coverage = TableMerge(
                    table="source_coverage",
                    source_rows=source_rows,
                    target_rows_before=before,
                    inserted=after - before,
                    skipped=max(0, source_rows - (after - before)),
                    target_rows_after=after,
                )
                by_name = {item.table: item for item in (coverage, *retained)}
                return MergeReport(tables=tuple(by_name[table] for table in ALL_TABLES))
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.execute("DETACH DATABASE source")


def _retain_target_tables(connection: sqlite3.Connection) -> tuple[TableMerge, ...]:
    """Report target-owned tables as kept whole, copying nothing from the source."""

    merges: list[TableMerge] = []
    for table in TARGET_RETAINED_KEYS:
        source_rows = count(connection, f'SELECT count(*) FROM source."{table}"')  # nosec B608
        target_rows = count(connection, f'SELECT count(*) FROM main."{table}"')  # nosec B608
        merges.append(
            TableMerge(
                table=table,
                source_rows=source_rows,
                target_rows_before=target_rows,
                inserted=0,
                skipped=source_rows,
                target_rows_after=target_rows,
            )
        )
    return tuple(merges)


def _require_no_overclaimed_coverage(connection: sqlite3.Connection) -> None:
    """Validate both ledgers against hydrated target facts before the union.

    The source may be dehydrated, so its fact tables are not counted. A larger source
    claim must not disappear when an existing target row wins on the same coverage key.
    Lower counts can be raised after the union; counts beyond the target facts are
    refused rather than reduced to whatever happens to be locally available.
    """

    _require_fin_summary_coverage_states(connection, schema="source")
    _require_fin_summary_coverage_states(connection, schema="main")
    _require_matching_coverage_counts(connection)
    for schema in ("main", "source"):
        for coverage_key, start, end, claim in _clean_fin_summary_claims(connection, schema=schema):
            actual = _fin_summary_range_count(connection, schema="main", start=start, end=end)
            if claim > actual:
                if schema == "main":
                    raise MergeError(
                        "jquants_fin_summaries coverage claims more rows than the store holds for "
                        f"'jquants_fin_summaries', {coverage_key!r}: claims {claim}, holds {actual}"
                    )
                raise MergeError(
                    f"jquants_fin_summaries coverage claims {claim} row(s) for {coverage_key!r} "
                    f"but the target holds {actual}; "
                    "hydrate from the published release before merging"
                )


def _require_matching_coverage_counts(connection: sqlite3.Connection) -> None:
    """Only clean financial-summary claims may reconcile differing counts."""
    row = connection.execute(
        "SELECT s.source, s.coverage_key "
        "FROM source.source_coverage s JOIN main.source_coverage t "
        "ON t.source = s.source AND t.coverage_key = s.coverage_key "
        "WHERE s.source NOT IN ('jquants_short_sale_reports', 'edinet_research_facts') "
        "AND s.record_count IS NOT t.record_count "
        "AND NOT (s.source = 'jquants_fin_summaries' AND s.status = 'ok' AND t.status = 'ok') "
        "ORDER BY s.source, s.coverage_key LIMIT 1"
    ).fetchone()
    if row is not None:
        raise MergeError(f"source_coverage payload disagrees for shared key: {row!r}")


def _coverage_overlaps(
    connection: sqlite3.Connection, *, schema: str
) -> set[tuple[str, str, str, str]]:
    attached = schema_name(schema)
    rows = connection.execute(
        "SELECT ok.source, ok.coverage_key, bad.coverage_key, bad.status "
        f"FROM {attached}.source_coverage ok "  # nosec B608
        f"JOIN {attached}.source_coverage bad ON bad.source = ok.source "
        "WHERE ok.status = 'ok' AND bad.status IN ('failed', 'partial') "
        "AND ok.source <> 'edinet_research_facts' "
        "AND ok.coverage_start IS NOT NULL AND ok.coverage_end IS NOT NULL "
        "AND bad.coverage_start IS NOT NULL AND bad.coverage_end IS NOT NULL "
        "AND ok.coverage_start <= bad.coverage_end "
        "AND ok.coverage_end >= bad.coverage_start"
    ).fetchall()
    return {
        (str(source), str(clean_key), str(failed_key), str(status))
        for source, clean_key, failed_key, status in rows
    }


def _require_no_reinstated_coverage(
    connection: sqlite3.Connection, *, known_overlaps: set[tuple[str, str, str, str]]
) -> None:
    """Refuse new overlap introduced by combining different acquisition histories.

    A writer can retain a wide partial complaint after repairing only its interior.
    Such an overlap already present in either input is not caused by this merge.
    Shared-key payload checks keep its ranges and statuses unchanged. A new pairing
    can instead revive a clean window that the other writer had retracted.
    """
    introduced = _coverage_overlaps(connection, schema="main") - known_overlaps
    if introduced:
        source, clean_key, failed_key, status = min(introduced)
        raise MergeError(
            f"{source} coverage {clean_key!r} would cover the range {failed_key!r} "
            f"recorded as {status}; the merge would reinstate a window a fetch retracted"
        )


def _merge_edinet_fact_coverage(connection: sqlite3.Connection) -> None:
    """Keep target claims with their facts; imported-only documents require recheck.

    These are per-document extraction results, not date ranges. A different document
    can fail on the same day. The source may be dehydrated, so its success/revision
    cannot prove which facts the target holds. Preserve target claims (including
    failures and newer extractor revisions); import missing IDs as retry work instead
    of borrowing a success claim. The global initialization marker is transferable.
    """
    connection.execute(
        "INSERT OR IGNORE INTO main.source_coverage "
        "(source, coverage_key, coverage_start, coverage_end, fetched_at_utc, "
        "record_count, status, error) "
        "SELECT source, coverage_key, coverage_start, coverage_end, fetched_at_utc, "
        "CASE WHEN coverage_key = 'initialized' THEN record_count ELSE 0 END, "
        "CASE WHEN coverage_key = 'initialized' THEN status ELSE 'failed' END, "
        "CASE WHEN coverage_key = 'initialized' THEN error "
        "ELSE 'imported_document_requires_recheck' END "
        "FROM source.source_coverage WHERE source = 'edinet_research_facts'"
    )


def _merge_short_sale_coverage(connection: sqlite3.Connection) -> None:
    """Select the latest claim per disclosure date, and carry it whole.

    Each disclosure date is a correction-prone complete snapshot, so its claim is chosen
    rather than unioned. A newer partial fetch can replace the stored rows; an older
    success must not relabel that incomplete correction as complete.

    The count travels with the claim it belongs to rather than being recounted here. It
    records what that fetch returned, and the rows are the release's — a target filled
    from a release that predates the fetch does not hold them yet, so recounting would
    rewrite "this date disclosed two positions" into "this date disclosed none". Zero and
    not-yet-visible are the one pair this dataset must never conflate.
    """

    source_claims = _short_coverage_claims(connection, schema="source")
    target_claims = _short_coverage_claims(connection, schema="main")
    selected = {
        disclosed_at: _select_short_claim(
            source_claims.get(disclosed_at), target_claims.get(disclosed_at)
        )
        for disclosed_at in sorted(set(source_claims) | set(target_claims))
    }
    # Checked against the union rather than against the target alone. The rows arrive by
    # hydration and the claims arrive by this merge, so a date the cloud fetched and
    # published reaches the target's table one step before it reaches the target's
    # ledger; refusing on the target's own claims would reject that ordinary sequence.
    _require_no_orphan_short_rows(connection, claims=selected)

    connection.execute(
        "DELETE FROM main.source_coverage WHERE source = ?", (_SHORT_SALE_REPORT_SOURCE,)
    )
    for disclosed_at, claim in selected.items():
        iso = disclosed_at.isoformat()
        connection.execute(
            """
            INSERT INTO main.source_coverage(
              source, coverage_key, coverage_start, coverage_end,
              fetched_at_utc, record_count, status, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _SHORT_SALE_REPORT_SOURCE,
                f"get_mkt_short_sale_report:{iso}..{iso}",
                iso,
                iso,
                claim.fetched_at_text,
                claim.record_count,
                claim.status,
                claim.error,
            ),
        )


def _short_coverage_claims(
    connection: sqlite3.Connection, *, schema: str
) -> dict[date, _ShortCoverageClaim]:
    rows = connection.execute(
        f"SELECT coverage_start, coverage_end, fetched_at_utc, "  # nosec B608
        f"record_count, status, error "
        f"FROM {schema_name(schema)}.source_coverage "
        "WHERE source = 'jquants_short_sale_reports'"
    ).fetchall()
    claims: dict[date, _ShortCoverageClaim] = {}
    for raw_start, raw_end, raw_fetched, raw_count, raw_status, raw_error in rows:
        # Every short-sale claim is rebuilt from the selected set, so a row this reader
        # skips is a row the rebuild drops. Nothing here may filter; a claim it cannot
        # place is refused instead.
        if raw_start is None or raw_end is None:
            raise MergeError("short-sale coverage has no disclosure date range")
        try:
            start = date.fromisoformat(str(raw_start))
            end = date.fromisoformat(str(raw_end))
            fetched = datetime.fromisoformat(str(raw_fetched))
        except ValueError as exc:
            raise MergeError("invalid short-sale coverage timestamp") from exc
        if fetched.tzinfo is None or fetched.utcoffset() is None:
            raise MergeError("invalid short-sale coverage range")
        # One claim, one disclosure date. The writer records the range it fetched a day
        # at a time, and the count belongs to that day; a claim spanning several days
        # carries one total that cannot be divided among them without inventing the
        # split, so it is refused rather than expanded.
        if start != end:
            raise MergeError(
                f"short-sale coverage spans more than one disclosure date: {start}..{end}"
            )
        if not isinstance(raw_count, int) or raw_count < 0:
            raise MergeError(f"short-sale coverage has no usable record count: {start}")
        claim = _ShortCoverageClaim(
            fetched_at=fetched,
            fetched_at_text=str(raw_fetched),
            record_count=raw_count,
            status=str(raw_status),
            error=str(raw_error) if raw_error is not None else None,
        )
        current = claims.get(start)
        if current is None or claim.fetched_at >= current.fetched_at:
            claims[start] = claim
    return claims


def _select_short_claim(
    source: _ShortCoverageClaim | None,
    target: _ShortCoverageClaim | None,
) -> _ShortCoverageClaim:
    if source is None:
        assert target is not None
        return target
    if target is None:
        return source
    return source if source.fetched_at > target.fetched_at else target


def _require_no_orphan_short_rows(
    connection: sqlite3.Connection,
    *,
    claims: Mapping[date, _ShortCoverageClaim],
) -> None:
    rows = connection.execute(
        "SELECT DISTINCT disclosed_at FROM main.jquants_short_sale_reports"
    ).fetchall()
    missing = sorted(str(raw) for (raw,) in rows if date.fromisoformat(str(raw)) not in claims)
    if missing:
        raise MergeError(f"jquants_short_sale_reports rows lack coverage: {missing[0]}")


def _require_fin_summary_coverage_states(connection: sqlite3.Connection, *, schema: str) -> None:
    """Classify every financial-summary claim so no hybrid state bypasses validation."""

    rows = connection.execute(
        f"SELECT coverage_key, coverage_start, coverage_end, record_count, "  # nosec B608
        f'status, error FROM {schema_name(schema)}."source_coverage" '
        "WHERE source = 'jquants_fin_summaries' ORDER BY coverage_key"
    ).fetchall()
    for coverage_key, start, end, record_count, status, error in rows:
        key = f"'jquants_fin_summaries', {coverage_key!r}"
        common_valid = (
            start is not None
            and end is not None
            and coverage_key == f"get_fin_summary_range:{start}..{end}"
            and isinstance(record_count, int)
            and record_count >= 0
        )
        state_valid = (status == "ok" and error is None) or (
            status in {"partial", "failed"} and isinstance(error, str) and bool(error.strip())
        )
        if not common_valid or not state_valid:
            raise MergeError(f"source_coverage payload disagrees for shared key: {key}")


def _clean_fin_summary_claims(
    connection: sqlite3.Connection, *, schema: str
) -> list[tuple[str, str, str, int]]:
    """Clean claims from the inputs already validated before this transaction's union."""

    rows = connection.execute(
        f"SELECT coverage_key, coverage_start, coverage_end, record_count "  # nosec B608
        f'FROM {schema_name(schema)}."source_coverage" '
        "WHERE source = 'jquants_fin_summaries' AND status = 'ok' AND error IS NULL "
        "ORDER BY coverage_key"
    ).fetchall()
    return [
        (str(coverage_key), str(start), str(end), record_count)
        for coverage_key, start, end, record_count in rows
    ]


def _raise_fin_summary_coverage_counts(connection: sqlite3.Connection) -> None:
    """Align clean claims with target rows after the validated coverage union.

    Both inputs were checked before the union in this transaction; neither the union
    nor the specialized coverage merges change financial fact rows. Counts may only
    rise, so a retained target claim cannot hide an overclaim from the source.
    """
    for coverage_key, start, end, record_count in _clean_fin_summary_claims(
        connection, schema="main"
    ):
        actual = _fin_summary_range_count(connection, schema="main", start=start, end=end)
        if actual <= record_count:
            continue
        connection.execute(
            "UPDATE main.source_coverage SET record_count = ? "
            "WHERE source = 'jquants_fin_summaries' AND coverage_key = ?",
            (actual, coverage_key),
        )


def _fin_summary_range_count(
    connection: sqlite3.Connection, *, schema: str, start: str, end: str
) -> int:
    row = connection.execute(
        f'SELECT count(*) FROM {schema_name(schema)}."jquants_fin_summaries" '  # nosec B608
        "WHERE disclosed_at BETWEEN ? AND ?",
        (start, end),
    ).fetchone()
    if row is None:
        raise MergeError("financial-summary coverage count returned no row")
    return int(row[0])


def _require_schema(path: Path) -> None:
    """Open the store on its own to check its shape.

    The market schema validator reads the connection's own tables, so it cannot speak
    about an attached database. Checking each store before the attach keeps the contract
    exact instead of settling for the version number alone.
    """

    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        version = count(connection, "PRAGMA user_version")
        if version != SQLITE_SCHEMA_VERSION:
            raise MergeError(
                f"market store schema is {version} but this code expects "
                f"{SQLITE_SCHEMA_VERSION}: {path}"
            )
        try:
            validate_current_schema(connection)
        except SQLiteSchemaError as error:
            raise MergeError(f"market store schema contract is invalid: {path}: {error}") from error


def _require_attached_version(connection: sqlite3.Connection, *, path: Path, schema: str) -> None:
    """Re-read the version through the attachment the merge will actually write from.

    The shape was checked on a separate connection. This catches a changed version,
    but does not prove file identity or detect replacement by another same-version
    file. Callers must keep their staged inputs stable throughout the merge.
    """

    version = count(connection, f"PRAGMA {schema_name(schema)}.user_version")
    if version != SQLITE_SCHEMA_VERSION:
        raise MergeError(
            f"market store schema changed between opening and attaching: {path} is {version}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="store to merge from")
    parser.add_argument("--target", type=Path, required=True, help="store to merge into")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = merge_stores(args.source, args.target)
    except MergeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(report.render())
    print(f"merged {report.inserted} rows into {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
