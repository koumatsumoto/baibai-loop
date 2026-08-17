"""Merge the market store's own tables from one copy into another, losing no row.

The market store has two writers. The daily batch extends it forward in the cloud, and
an operator extends it backward locally. Most of what they write is no longer decided
here: the fifteen fetch-derived tables are published to the L1 lake, and the object this
merge runs against is the copy that carries only what the lake does not own. Their
reconciliation is the release's, not this file's — ``publish_market_lake`` refuses to
build on a release the lake has moved past, and ``lake dehydrate`` refuses to empty a
store whose rows the release does not account for. What is left here is the four tables
that stay canonical in SQLite.

``source_coverage`` is the ledger of what was fetched, and both writers keep their own.
Its rows describe lake-owned facts but are not facts themselves: a claim carries a status
and an error the lake does not represent, and it is what decides whether a range gets
fetched again. Clean financial-summary ranges state a row count, and a count is only
provable where the rows are — in a hydrated store, never in the emptied copy that
travels. So the source's counts are taken as claims, and this target's are only ever
lifted to the rows it holds, never lowered to them.

Only the financial-summary windows are lifted and proved. Daily-bar windows carry their
claim verbatim, and that is the safer half of the asymmetry rather than an omission: the
wedge above comes from writing a *smaller* number, and nothing here writes one. A bar
window that outruns this store is left standing, `verify-cache-coverage` refuses the
operator's own screening run until they fill from the serving release, and the fill makes
the two agree.

The ledger is not append-only. A failed re-fetch cuts its range out of the overlapping
``ok`` windows and rewrites the survivors under new keys, so that the gap is visible
where the fetch failed. A union by key can therefore reinstate a wide window a later
fetch retracted, and the merge refuses that rather than repairing it: an ``ok`` window
overlapping a ``failed`` or ``partial`` one for the same source is a ledger no fetcher
would write.

The other three tables are functions of other tables rather than accumulations of fetched
records. Only the operator derives them, so the copy that ran the derivation last holds
the answer and the target is kept whole.

Both stores must carry the current schema. An older cloud copy is not migrated here —
the cloud raises its own schema by opening the store, and doing it from this side would
publish a shape the cloud has never written.
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

from baibai_batch.storage.store_merge import (
    MergeError,
    MergeReport,
    RowFilter,
    TableMerge,
    count,
    merge_fact_tables,
    schema_name,
)
from baibai_engine.batch_api import (
    MARKET_SCHEMA_VERSION as SQLITE_SCHEMA_VERSION,
)
from baibai_engine.batch_api import (
    MarketSchemaError as SQLiteSchemaError,
)
from baibai_engine.batch_api import (
    validate_market_schema as validate_current_schema,
)

# The ledger of fetches, with the key that decides whether a row is the same row. It is
# the one accumulating table the lake does not own, so it is the one this merge unions.
FACT_KEYS: Mapping[str, tuple[str, ...]] = {
    "source_coverage": ("source", "coverage_key"),
}

# Tables that are a function of other tables rather than an accumulation of fetched
# records. Only the operator derives them, so the store that ran the derivation last
# holds the answer and the target is kept whole. Merging them by key would do two wrong
# things: a row a later derivation retracted — an offer that turned out to have a second
# bidder, a company the exchange removed from its list — would be reinserted from the
# older copy, and a corrected value would read as two stores disagreeing and refuse the
# whole publish. Both are silent, and the first one puts a price into the calibration
# forward that the current rules say cannot be established.
DERIVED_KEYS: Mapping[str, tuple[str, ...]] = {
    "jpx_delistings": ("delisted_on", "ticker"),
    "tender_offer_exit_values": ("ticker", "delisted_on"),
    "tse_capital_policy_snapshots": ("snapshot_month_end", "ticker"),
}

ALL_TABLES: Mapping[str, tuple[str, ...]] = {**FACT_KEYS, **DERIVED_KEYS}

# When a store read the source, not what the source said. Two stores that read the same
# range at different moments differ here by construction — measured on the real stores,
# 218 of 220 disagreements were nothing but `fetched_at_utc` — so comparing it would
# refuse every merge. The insert leaves the target's reading in place.
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
_NON_SHORT_COVERAGE = RowFilter(
    unaliased="source <> 'jquants_short_sale_reports'",
    aliased="s.source <> 'jquants_short_sale_reports'",
)


@dataclass(frozen=True, slots=True)
class _ShortCoverageClaim:
    fetched_at: datetime
    fetched_at_text: str
    record_count: int
    status: str
    error: str | None


def merge_stores(source: Path, target: Path) -> MergeReport:
    """Insert every source row the target lacks, and verify none is left behind."""

    for path in (source, target):
        if not path.is_file():
            raise MergeError(f"market store does not exist: {path}")
    _require_schema(source)
    _require_schema(target)
    uri = f"{target.resolve().as_uri()}"
    with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as connection:
        connection.execute("ATTACH DATABASE ? AS source", (f"{source.resolve().as_uri()}?mode=ro",))
        try:
            _require_attached_version(connection, path=source, schema="source")
            # No separate column comparison: the shape validator above pins every column
            # of every table on both stores, so two stores that reach here cannot differ.
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            try:
                # The table is merged in two passes — short-sale claims are chosen per
                # disclosure date, everything else is unioned by key — so the report row
                # is built from counts taken around both rather than from either half.
                source_rows = count(connection, "SELECT count(*) FROM source.source_coverage")
                before = count(connection, "SELECT count(*) FROM main.source_coverage")
                _require_no_overclaimed_coverage(connection)
                _merge_short_sale_coverage(connection)
                merge_fact_tables(
                    connection,
                    {"source_coverage": FACT_KEYS["source_coverage"]},
                    eligible=_NON_SHORT_COVERAGE,
                    uncompared=_MERGE_EXEMPTIONS,
                )
                _raise_fin_summary_coverage_counts(connection)
                _require_fin_summary_coverage_counts(connection, schema="main")
                _require_no_reinstated_coverage(connection)
                derived = _retain_derived_tables(connection)
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
                by_name = {item.table: item for item in (coverage, *derived)}
                return MergeReport(tables=tuple(by_name[table] for table in ALL_TABLES))
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.execute("DETACH DATABASE source")


def _retain_derived_tables(connection: sqlite3.Connection) -> tuple[TableMerge, ...]:
    """Report the derived tables as kept whole, copying nothing from the source."""

    merges: list[TableMerge] = []
    for table in DERIVED_KEYS:
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
    """Refuse a target claim that asserts more rows than the target holds.

    Only the target is checked. The source is the copy R2 carries, whose lake-owned
    tables were emptied by the publication that put those rows in the release, so every
    claim in it would read as an overclaim about a file that was never meant to hold
    the rows.

    The check is one-directional because the two errors are not symmetric. A claim above
    the rows describes a store that is not this one, and the reconcile below would lower
    it to what this store holds — quietly replacing the record of what was fetched with
    a smaller number nobody asked for. A claim below the rows is an ordinary state after
    the cutover: the rows arrive by hydration and the ledger arrives by this merge, so a
    store filled from a release newer than its own ledger holds more than it claims until
    the reconcile raises the claim.

    Refusing the dangerous direction is also what requires the target to be hydrated. A
    store whose claims say tens of thousands of rows and whose tables are empty fails
    here rather than having its claims quietly rewritten down to zero.
    """

    _require_fin_summary_coverage_states(connection, schema="source")
    _require_fin_summary_coverage_states(connection, schema="main")
    for coverage_key, start, end, claim in _clean_fin_summary_claims(connection, schema="main"):
        actual = _fin_summary_range_count(connection, schema="main", start=start, end=end)
        if claim > actual:
            raise MergeError(
                "jquants_fin_summaries coverage claims more rows than the store holds for "
                f"'jquants_fin_summaries', {coverage_key!r}: claims {claim}, holds {actual}"
            )


def _require_no_reinstated_coverage(connection: sqlite3.Connection) -> None:
    """Refuse a ledger where a clean window covers a range a fetch recorded as failed.

    No fetcher writes that: `record_range_source_coverage` cuts the failed range out of
    every overlapping `ok` window before it records the failure, so the gap shows. Only a
    key-wise union can put the wide window back — the older copy still holds it — and the
    result reads as covered exactly where the other writer proved it is not.
    """

    row = connection.execute(
        "SELECT ok.source, ok.coverage_key, bad.coverage_key, bad.status "
        "FROM main.source_coverage ok "
        "JOIN main.source_coverage bad ON bad.source = ok.source "
        "WHERE ok.status = 'ok' AND bad.status IN ('failed', 'partial') "
        "AND ok.coverage_start IS NOT NULL AND ok.coverage_end IS NOT NULL "
        "AND bad.coverage_start IS NOT NULL AND bad.coverage_end IS NOT NULL "
        "AND ok.coverage_start <= bad.coverage_end "
        "AND ok.coverage_end >= bad.coverage_start "
        "ORDER BY ok.source, ok.coverage_key LIMIT 1"
    ).fetchone()
    if row is not None:
        source, clean_key, failed_key, status = row
        raise MergeError(
            f"{source} coverage {clean_key!r} would cover the range {failed_key!r} "
            f"recorded as {status}; the merge would reinstate a window a fetch retracted"
        )


def _merge_short_sale_coverage(connection: sqlite3.Connection) -> None:
    """Select the better claim per disclosure date, and carry it whole.

    Each disclosure date is a correction-prone complete snapshot, so its claim is chosen
    rather than unioned: a successful fetch beats an incomplete one whatever their ages,
    and between two of the same kind the newer one wins.

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
    if source.status == "ok" and target.status != "ok":
        return source
    if target.status == "ok" and source.status != "ok":
        return target
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
    """Every clean financial-summary range claim, with its shape already validated."""

    rows = connection.execute(
        f"SELECT coverage_key, coverage_start, coverage_end, record_count "  # nosec B608
        f'FROM {schema_name(schema)}."source_coverage" '
        "WHERE source = 'jquants_fin_summaries' AND status = 'ok' AND error IS NULL "
        "ORDER BY coverage_key"
    ).fetchall()
    claims: list[tuple[str, str, str, int]] = []
    for coverage_key, start, end, record_count in rows:
        if (
            start is None
            or end is None
            or coverage_key != f"get_fin_summary_range:{start}..{end}"
            or not isinstance(record_count, int)
            or record_count < 0
        ):
            key = f"'jquants_fin_summaries', {coverage_key!r}"
            raise MergeError(f"source_coverage payload disagrees for shared key: {key}")
        claims.append((str(coverage_key), str(start), str(end), record_count))
    return claims


def _require_fin_summary_coverage_counts(connection: sqlite3.Connection, *, schema: str) -> None:
    """Prove every clean range claim against the facts held by that store."""

    for coverage_key, start, end, record_count in _clean_fin_summary_claims(
        connection, schema=schema
    ):
        actual = _fin_summary_range_count(connection, schema=schema, start=start, end=end)
        if actual != record_count:
            raise MergeError(
                "jquants_fin_summaries coverage claims "  # nosec B608 - no SQL here
                f"{record_count} row(s) for {coverage_key!r} but the store holds {actual}; "
                "the published ledger describes a release this store was not filled from, "
                "so hydrate from the release the lake serves and merge again"
            )


def _raise_fin_summary_coverage_counts(connection: sqlite3.Connection) -> None:
    """Lift a clean claim that understates the rows this target holds, and only that.

    A claim carried over from the source states a count proved against a store that is
    not this one. Where this target already holds more rows than the claim names, the
    claim is behind and is lifted, which is what keeps the ledger describing the store a
    reader will meet.

    Lowering is the direction this must never take. A claim above this target's rows is
    describing a release this store has not been filled from — the cloud's ledger of a
    generation published after the operator hydrated — and writing the smaller number in
    would ship a ledger that understates the release. The next fill restores the rows,
    the ledger keeps the smaller number, and `verify-cache-coverage` then refuses every
    screening run while no re-fetch is ever planned: `covered_intervals` drops a window
    only when its count is zero, so an understated non-zero window still reads as held.
    The post-merge proof turns that case into a refusal here instead.
    """

    rows = connection.execute(
        "SELECT coverage_key, coverage_start, coverage_end, record_count "
        "FROM main.source_coverage "
        "WHERE source = 'jquants_fin_summaries' AND status = 'ok' AND error IS NULL "
        "ORDER BY coverage_key"
    ).fetchall()
    for coverage_key, start, end, record_count in rows:
        if start is None or end is None:
            key = f"'jquants_fin_summaries', {coverage_key!r}"
            raise MergeError(f"source_coverage payload disagrees for shared key: {key}")
        actual = _fin_summary_range_count(connection, schema="main", start=str(start), end=str(end))
        if actual <= int(record_count or 0):
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

    The shape was checked on a separate connection, so this is what ties that check to
    this one: a path that resolved to a different file between the two opens shows up
    here as a version that no longer matches.
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
