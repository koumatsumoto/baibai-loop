"""Merge one market store into another, keeping every row both sides hold.

The market store has two writers. The daily batch extends it forward in the cloud, and
an operator extends it backward locally — a decade of bars is hours of provider calls,
so the deep history is fetched once on a machine that can take the time. Each side
therefore holds rows the other has never seen, and publishing the local store means
merging the cloud copy into it first and proving that nothing cloud-side is dropped.

Most tables here are appendable facts with a key. ``source_coverage`` carries both
single-day keys and range claims. A clean financial-summary range is valid only while
its stored count matches the rows in that store, so input claims are recounted before
the union and target claims are regenerated from the resulting rows. Short-sale reports
are the exception: each disclosure date is a correction-prone complete snapshot, so the
newer complete fetch replaces that date while response-order differences alone are
ignored. Failure provenance remains explicit and is never a completeness claim.

The EDINET document index is the one table the merge writes into rather than only adds
to. That index is a view that decays: once a filing's public-inspection period ends the
day's list still returns the entry with its company, form, timestamp, title and parent
nulled, so a copy that read the day later holds strictly less than one that read it
earlier, and no fetch can recover the difference. The merge therefore gives the target
back every such value the source still holds before comparing the two.

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
from datetime import date, datetime, timedelta
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
    EDINET_DOCUMENT_DESCRIPTIVE_COLUMNS,
    EDINET_DOCUMENT_LIFECYCLE_COLUMNS,
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

# Every table in the store, with the key that decides whether a row is the same row.
# Listed rather than read from the schema: a new table has to be classified by someone
# who knows whether its rows accumulate, and an unlisted one would otherwise be dropped
# from the merge in silence. `test_every_market_table_is_merged` holds the two together.
FACT_KEYS: Mapping[str, tuple[str, ...]] = {
    "edinet_buyback_reports": ("ticker", "report_month_end"),
    "edinet_document_lists": ("doc_date",),
    "edinet_documents": ("doc_date", "sequence_number"),
    "edinet_metrics": ("asof_date", "ticker"),
    "jpx_regulation_flags": ("asof_date", "source_name", "ticker", "flag"),
    "jpx_regulation_sources": ("asof_date", "source_name"),
    "jquants_daily_bars": ("ticker", "traded_at"),
    "jquants_earnings_calendar": ("announcement_date", "ticker"),
    "jquants_fin_summaries": ("ticker", "disclosed_at"),
    "jquants_market_calendar": ("day",),
    "jquants_margin_alerts": ("publication_date", "ticker"),
    "jquants_all_issues_daily_margin": ("balance_date", "ticker"),
    "jquants_master_snapshots": ("snapshot_date", "ticker"),
    "jquants_short_sale_reports": ("disclosed_at", "source_ordinal"),
    "jquants_weekly_margin": ("week_end", "ticker"),
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

# Columns that record how and when a store read the source, not what the source said.
# Two stores that read the same record at different moments differ on these by
# construction — measured on the real stores, 218 of 220 disagreements were nothing but
# `fetched_at_utc` — so comparing them would refuse every merge. EDINET is the reason the
# other two are here: it revises a document's edit status and finalises a day's list after
# first publishing them, so the later read carries a different marker for the same
# document. The filing lifecycle columns join them for the same reason: the inspection
# period runs out, the document files stop being downloadable, and a filing can be
# withdrawn, all after the day was first read, so two copies of the same row differ by
# how long ago each was read. `extractor_revision` identifies the local reader
# implementation rather than an assertion in that document. The insert leaves the
# target's reading metadata in place. Everything the source actually asserts — prices,
# financials, holdings, coverage extents and counts — stays compared.
UNCOMPARED: Mapping[str, tuple[str, ...]] = {
    "edinet_document_lists": ("process_datetime", "fetched_at_utc", "is_final"),
    "edinet_documents": ("doc_info_edit_status", *EDINET_DOCUMENT_LIFECYCLE_COLUMNS),
    "edinet_metrics": ("extractor_revision",),
    "jpx_regulation_flags": ("fetched_at_utc",),
    "jpx_regulation_sources": ("fetched_at_utc",),
    "source_coverage": ("fetched_at_utc",),
}

# The cloud copy can carry the current columns without having fetched their values yet.
# A fully rebuilt local target may enrich only these fields. The directional comparison
# still refuses a populated source against a missing target and any two populated values
# that disagree, so a local cache that is not fully rebuilt cannot claim completeness.
SOURCE_MISSING_ALLOWED: Mapping[str, tuple[str, ...]] = {
    "jquants_fin_summaries": (
        "forecast_profit",
        "forecast_ordinary_profit",
        "treasury_shares",
        "equity_to_asset_ratio",
        "dividend_q1",
        "dividend_interim",
        "dividend_q3",
        "dividend_year_end",
        "dividend_total_annual",
        "average_shares",
    ),
    # The submitter and target company of a filing were added to the index after the
    # published copy had already stored those days, and the daily refresh only rewrites
    # the current day — so every historical row on the published side carries nulls that
    # only `backfill-edinet-identity` fills, and only on the operator's store. Comparing
    # them strictly would refuse every publish with no way to advance the published copy.
    # The descriptive columns are here because EDINET stops serving them once a filing's
    # inspection period ends, and `_restore_expired_document_descriptions` has already
    # given the target every value the source still had. A remaining null on the source
    # side is therefore either that expiry or a filing that genuinely has no securities
    # code and no parent, and neither is a reason to erase what the target observed.
    "edinet_documents": (
        "edinet_code",
        "issuer_edinet_code",
        "subject_edinet_code",
        *EDINET_DOCUMENT_DESCRIPTIVE_COLUMNS,
    ),
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


# Give the target back every description the source still holds. Both copies read the
# same days, and EDINET nulls these five once a filing's inspection period ends, so a
# null on one side and a value on the other means one copy read the day while the filing
# was still served. The value was true then and stays true, and neither copy can fetch it
# again. `doc_id` has to agree as well as the key: a day EDINET returned in a different
# order would otherwise graft one filing's description onto another, and leaving those
# rows alone lets the strict `doc_id` comparison refuse the merge instead.
_RESTORE_EDINET_DESCRIPTIONS = """
UPDATE main.edinet_documents AS t
SET sec_code = COALESCE(t.sec_code, s.sec_code),
    doc_type_code = COALESCE(t.doc_type_code, s.doc_type_code),
    parent_doc_id = COALESCE(t.parent_doc_id, s.parent_doc_id),
    submit_datetime = COALESCE(t.submit_datetime, s.submit_datetime),
    doc_description = COALESCE(t.doc_description, s.doc_description)
FROM source.edinet_documents AS s
WHERE s.doc_date = t.doc_date
  AND s.sequence_number = t.sequence_number
  AND s.doc_id = t.doc_id
  AND ((t.sec_code IS NULL AND s.sec_code IS NOT NULL)
    OR (t.doc_type_code IS NULL AND s.doc_type_code IS NOT NULL)
    OR (t.parent_doc_id IS NULL AND s.parent_doc_id IS NOT NULL)
    OR (t.submit_datetime IS NULL AND s.submit_datetime IS NOT NULL)
    OR (t.doc_description IS NULL AND s.doc_description IS NOT NULL))
"""


# The two copies read a shared day at different moments, so the lifecycle columns can
# hold two honest answers and the merge keeps the target's. That is silent by
# construction, and the reading it drops can be the newer one — the source finalising a
# day the target had already read, for instance. Counting the rows makes a day worth
# re-listing visible instead of leaving the operator to notice a filing quietly missing
# from an index.
_COUNT_LIFECYCLE_DISAGREEMENTS = """
SELECT count(*)
FROM source.edinet_documents s
JOIN main.edinet_documents t
  ON s.doc_date = t.doc_date AND s.sequence_number = t.sequence_number
WHERE s.legal_status IS NOT t.legal_status
   OR s.withdrawal_status IS NOT t.withdrawal_status
   OR s.csv_flag IS NOT t.csv_flag
   OR s.xbrl_flag IS NOT t.xbrl_flag
"""


@dataclass(frozen=True, slots=True)
class MarketMergeReport(MergeReport):
    """The merge result, plus what it did with the two copies of the EDINET index."""

    restored_document_descriptions: int = 0
    discarded_lifecycle_readings: int = 0

    def notes(self) -> tuple[str, ...]:
        # Always printed, so that a run which restored nothing is distinguishable from
        # one where the restore never ran.
        return (
            (
                "edinet_documents rows whose expired descriptions the source restored: "
                f"{self.restored_document_descriptions}"
            ),
            (
                "edinet_documents rows where the source read a different filing lifecycle "
                f"and the target's reading was kept: {self.discarded_lifecycle_readings}"
            ),
        )


@dataclass(frozen=True, slots=True)
class _ShortCoverageClaim:
    fetched_at: datetime
    fetched_at_text: str
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
                _require_compatible_source_coverage_counts(connection)
                restored = _restore_expired_document_descriptions(connection)
                lifecycle_disagreements = count(connection, _COUNT_LIFECYCLE_DISAGREEMENTS)
                short_reports = _merge_short_sale_report_snapshots(connection)
                ordinary_keys = {
                    table: keys
                    for table, keys in FACT_KEYS.items()
                    if table not in {"jquants_short_sale_reports", "source_coverage"}
                }
                ordinary_tables = merge_fact_tables(
                    connection,
                    ordinary_keys,
                    uncompared=_MERGE_EXEMPTIONS,
                    source_missing_allowed=SOURCE_MISSING_ALLOWED,
                )
                (coverage_table,) = merge_fact_tables(
                    connection,
                    {"source_coverage": FACT_KEYS["source_coverage"]},
                    eligible=_NON_SHORT_COVERAGE,
                    uncompared=_MERGE_EXEMPTIONS,
                )
                _reconcile_fin_summary_coverage_counts(connection)
                _require_compatible_source_coverage_counts(connection)
                derived = _retain_derived_tables(connection)
                connection.commit()
                by_name = {
                    item.table: item
                    for item in (*ordinary_tables, short_reports, coverage_table, *derived)
                }
                return MarketMergeReport(
                    tables=tuple(by_name[table] for table in ALL_TABLES),
                    restored_document_descriptions=restored,
                    discarded_lifecycle_readings=lifecycle_disagreements,
                )
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.execute("DETACH DATABASE source")


def _restore_expired_document_descriptions(connection: sqlite3.Connection) -> int:
    """Fill each target null the source still describes, and report how many rows moved."""

    return connection.execute(_RESTORE_EDINET_DESCRIPTIONS).rowcount


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


def _require_compatible_source_coverage_counts(connection: sqlite3.Connection) -> None:
    """Allow differing clean counts only when both inputs prove their range claims."""

    _require_fin_summary_coverage_states(connection, schema="source")
    _require_fin_summary_coverage_states(connection, schema="main")
    _require_fin_summary_coverage_counts(connection, schema="source")
    _require_fin_summary_coverage_counts(connection, schema="main")
    rows = connection.execute(
        "SELECT s.source, s.coverage_key, s.coverage_start, t.coverage_start, "
        "s.coverage_end, t.coverage_end, s.record_count, t.record_count, "
        "s.status, t.status, s.error, t.error "
        "FROM source.source_coverage s "
        "JOIN main.source_coverage t USING (source, coverage_key) "
        "WHERE s.source <> 'jquants_short_sale_reports' "
        "AND s.record_count IS NOT t.record_count "
        "ORDER BY s.source, s.coverage_key"
    ).fetchall()
    for row in rows:
        (
            source,
            coverage_key,
            source_start,
            target_start,
            source_end,
            target_end,
            source_count,
            target_count,
            source_status,
            target_status,
            source_error,
            target_error,
        ) = row
        key = f"{source!r}, {coverage_key!r}"
        if (
            source != "jquants_fin_summaries"
            or source_start != target_start
            or source_end != target_end
            or coverage_key != f"get_fin_summary_range:{source_start}..{source_end}"
            or source_status != "ok"
            or target_status != "ok"
            or source_error is not None
            or target_error is not None
            or not isinstance(source_count, int)
            or not isinstance(target_count, int)
            or source_count < 0
            or target_count < 0
            or source_start is None
            or source_end is None
        ):
            raise MergeError(f"source_coverage payload disagrees for shared key: {key}")
        actual_source = _fin_summary_range_count(
            connection, schema="source", start=str(source_start), end=str(source_end)
        )
        actual_target = _fin_summary_range_count(
            connection, schema="main", start=str(target_start), end=str(target_end)
        )
        if actual_source != source_count or actual_target != target_count:
            raise MergeError(
                "jquants_fin_summaries coverage count does not match stored rows for " + key
            )


def _merge_short_sale_report_snapshots(connection: sqlite3.Connection) -> TableMerge:
    """Merge correction-prone disclosure dates as complete fetched snapshots.

    Response order is provenance, not row identity. Equal row multisets therefore
    agree even when their ordinals differ. When the source has revised a date, the
    newer successful fetch replaces that entire target date; an incomplete fetch
    never supersedes a complete one.
    """

    source_rows = count(connection, 'SELECT COUNT(*) FROM source."jquants_short_sale_reports"')
    before = count(connection, 'SELECT COUNT(*) FROM main."jquants_short_sale_reports"')
    source_claims = _short_coverage_claims(connection, schema="source")
    target_claims = _short_coverage_claims(connection, schema="main")
    _require_no_orphan_short_rows(connection, schema="source", claims=source_claims)
    _require_no_orphan_short_rows(connection, schema="main", claims=target_claims)

    copied = 0
    selected_claims: dict[date, _ShortCoverageClaim] = {}
    for disclosed_at in sorted(set(source_claims) | set(target_claims)):
        source_claim = source_claims.get(disclosed_at)
        target_claim = target_claims.get(disclosed_at)
        selected = _select_short_claim(source_claim, target_claim)
        origin, claim = selected
        source_payload = (
            _short_day_payload(connection, schema="source", disclosed_at=disclosed_at)
            if source_claim is not None
            else None
        )
        target_payload = (
            _short_day_payload(connection, schema="main", disclosed_at=disclosed_at)
            if target_claim is not None
            else None
        )
        if (
            source_payload is not None
            and target_payload is not None
            and source_payload == target_payload
        ):
            origin = "target"
        elif (
            source_claim is not None
            and target_claim is not None
            and source_claim.status == target_claim.status
            and source_claim.fetched_at == target_claim.fetched_at
            and source_payload != target_payload
        ):
            raise MergeError(
                "jquants_short_sale_reports snapshots disagree at the same fetched_at: "
                + disclosed_at.isoformat()
            )
        if origin == "source":
            iso = disclosed_at.isoformat()
            connection.execute(
                "DELETE FROM main.jquants_short_sale_reports WHERE disclosed_at = ?", (iso,)
            )
            connection.execute(
                "INSERT INTO main.jquants_short_sale_reports "
                "SELECT * FROM source.jquants_short_sale_reports WHERE disclosed_at = ?",
                (iso,),
            )
            copied += len(source_payload or ())
        selected_claims[disclosed_at] = claim

    connection.execute(
        "DELETE FROM main.source_coverage WHERE source = ?", (_SHORT_SALE_REPORT_SOURCE,)
    )
    for disclosed_at, claim in selected_claims.items():
        iso = disclosed_at.isoformat()
        record_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM main.jquants_short_sale_reports WHERE disclosed_at = ?",
                (iso,),
            ).fetchone()[0]
            or 0
        )
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
                record_count,
                claim.status,
                claim.error,
            ),
        )
    after = count(connection, 'SELECT COUNT(*) FROM main."jquants_short_sale_reports"')
    return TableMerge(
        table="jquants_short_sale_reports",
        source_rows=source_rows,
        target_rows_before=before,
        inserted=copied,
        skipped=max(0, source_rows - copied),
        target_rows_after=after,
    )


def _short_coverage_claims(
    connection: sqlite3.Connection, *, schema: str
) -> dict[date, _ShortCoverageClaim]:
    rows = connection.execute(
        f"SELECT coverage_start, coverage_end, fetched_at_utc, status, error "  # nosec B608
        f"FROM {schema_name(schema)}.source_coverage "
        "WHERE source = 'jquants_short_sale_reports' "
        "AND coverage_start IS NOT NULL AND coverage_end IS NOT NULL"
    ).fetchall()
    claims: dict[date, _ShortCoverageClaim] = {}
    for raw_start, raw_end, raw_fetched, raw_status, raw_error in rows:
        try:
            start = date.fromisoformat(str(raw_start))
            end = date.fromisoformat(str(raw_end))
            fetched = datetime.fromisoformat(str(raw_fetched))
        except ValueError as exc:
            raise MergeError("invalid short-sale coverage timestamp") from exc
        if start > end or fetched.tzinfo is None or fetched.utcoffset() is None:
            raise MergeError("invalid short-sale coverage range")
        claim = _ShortCoverageClaim(
            fetched_at=fetched,
            fetched_at_text=str(raw_fetched),
            status=str(raw_status),
            error=str(raw_error) if raw_error is not None else None,
        )
        cursor = start
        while cursor <= end:
            current = claims.get(cursor)
            if current is None or claim.fetched_at >= current.fetched_at:
                claims[cursor] = claim
            cursor += timedelta(days=1)
    return claims


def _select_short_claim(
    source: _ShortCoverageClaim | None,
    target: _ShortCoverageClaim | None,
) -> tuple[str, _ShortCoverageClaim]:
    if source is None:
        assert target is not None
        return "target", target
    if target is None:
        return "source", source
    if source.status == "ok" and target.status != "ok":
        return "source", source
    if target.status == "ok" and source.status != "ok":
        return "target", target
    return ("source", source) if source.fetched_at > target.fetched_at else ("target", target)


def _short_day_payload(
    connection: sqlite3.Connection, *, schema: str, disclosed_at: date
) -> tuple[tuple[object, ...], ...]:
    rows = connection.execute(
        f"SELECT calculated_at, ticker, short_seller_name, "  # nosec B608
        "discretionary_investment_contractor_name, investment_fund_name, "
        "short_ratio, short_shares, short_trading_units, previous_reported_at, "
        "previous_short_ratio, is_cancellation, notes "
        f"FROM {schema_name(schema)}.jquants_short_sale_reports "
        "WHERE disclosed_at = ?",
        (disclosed_at.isoformat(),),
    ).fetchall()
    return tuple(sorted((tuple(row) for row in rows), key=repr))


def _require_no_orphan_short_rows(
    connection: sqlite3.Connection,
    *,
    schema: str,
    claims: Mapping[date, _ShortCoverageClaim],
) -> None:
    rows = connection.execute(
        f"SELECT DISTINCT disclosed_at "  # nosec B608
        f"FROM {schema_name(schema)}.jquants_short_sale_reports"
    ).fetchall()
    missing = [str(raw) for (raw,) in rows if date.fromisoformat(str(raw)) not in claims]
    if missing:
        raise MergeError(f"{schema} jquants_short_sale_reports rows lack coverage: {missing[0]}")


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


def _require_fin_summary_coverage_counts(connection: sqlite3.Connection, *, schema: str) -> None:
    """Prove every clean range claim against the facts held by that store."""

    rows = connection.execute(
        f"SELECT coverage_key, coverage_start, coverage_end, record_count "  # nosec B608
        f'FROM {schema_name(schema)}."source_coverage" '
        "WHERE source = 'jquants_fin_summaries' AND status = 'ok' AND error IS NULL "
        "ORDER BY coverage_key"
    ).fetchall()
    for coverage_key, start, end, record_count in rows:
        key = f"'jquants_fin_summaries', {coverage_key!r}"
        if (
            start is None
            or end is None
            or coverage_key != f"get_fin_summary_range:{start}..{end}"
            or not isinstance(record_count, int)
            or record_count < 0
        ):
            raise MergeError(f"source_coverage payload disagrees for shared key: {key}")
        actual = _fin_summary_range_count(connection, schema=schema, start=str(start), end=str(end))
        if actual != record_count:
            raise MergeError(
                "jquants_fin_summaries coverage count does not match stored rows for " + key
            )


def _reconcile_fin_summary_coverage_counts(connection: sqlite3.Connection) -> None:
    """Make clean target claims describe the fact union produced by this transaction."""

    rows = connection.execute(
        "SELECT coverage_key, coverage_start, coverage_end "
        "FROM main.source_coverage "
        "WHERE source = 'jquants_fin_summaries' AND status = 'ok' AND error IS NULL "
        "ORDER BY coverage_key"
    ).fetchall()
    for coverage_key, start, end in rows:
        if start is None or end is None:
            key = f"'jquants_fin_summaries', {coverage_key!r}"
            raise MergeError(f"source_coverage payload disagrees for shared key: {key}")
        actual = _fin_summary_range_count(connection, schema="main", start=str(start), end=str(end))
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
