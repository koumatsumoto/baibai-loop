"""Replay only TradingView receipts onto a fresh market store, preserving saved rows."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.jquants.dependencies import (
    dependency_covers_day,
    read_dependency,
)
from baibai_engine.data_collection.providers.jquants.master_native import (
    verify_migrated_master_receipts,
)
from baibai_engine.data_collection.providers.jquants.receipts import read_complete_request
from baibai_engine.data_collection.providers.tradingview.contract import BATCH_TOOL_IDENTIFIERS
from baibai_engine.data_collection.providers.tradingview.receipts import read_tool_results
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.observations.external_assessments.tradingview import (
    observations as observations_owner,
)
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.contract import (
    COLUMNS,
    TABLE,
)
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.decode import (
    decode_tool_result,
)
from baibai_engine.machine_analysis.observations.stocks.master_snapshot import (
    validate_master_snapshot,
)
from baibai_engine.storage.objects.r2 import ObjectStore
from baibai_engine.storage.sqlite.source_cache.schema import (
    open_connection,
    validate_current_schema,
)
from baibai_jobs.integration.replay_result import ReplayResult
from baibai_jobs.maintenance.tradingview_context import (
    calendar_context,
    snapshot_state,
    symbols_from_master_payloads,
    universe,
    validate_time,
)
from baibai_jobs.observability.tradingview import (
    SourceDataError,
    failure_category,
)


@dataclass(frozen=True, slots=True)
class TradingViewHandoff:
    snapshot_date: date
    master_asof: date
    symbols: tuple[str, ...]
    receipt_ids: tuple[str, ...]
    master_request_ids: tuple[str, ...]
    master_native_input: InputSnapshot | None = None
    calendar_native_input: InputSnapshot | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "snapshot_date": self.snapshot_date.isoformat(),
            "master_asof": self.master_asof.isoformat(),
            "symbols": list(self.symbols),
            "receipt_ids": list(self.receipt_ids),
            "master_request_ids": list(self.master_request_ids),
            **(
                {"master_native_input": self.master_native_input.model_dump(mode="json")}
                if self.master_native_input is not None
                else {}
            ),
            **(
                {"calendar_native_input": self.calendar_native_input.model_dump(mode="json")}
                if self.calendar_native_input is not None
                else {}
            ),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> TradingViewHandoff:
        for field in ("symbols", "receipt_ids", "master_request_ids"):
            items = value.get(field)
            if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
                raise ValueError(f"invalid TradingView handoff {field}")
        try:
            return cls(
                snapshot_date=date.fromisoformat(value["snapshot_date"]),
                master_asof=date.fromisoformat(value["master_asof"]),
                symbols=tuple(value["symbols"]),
                receipt_ids=tuple(value["receipt_ids"]),
                master_request_ids=tuple(value["master_request_ids"]),
                master_native_input=InputSnapshot.model_validate(value["master_native_input"])
                if value.get("master_native_input") is not None
                else None,
                calendar_native_input=InputSnapshot.model_validate(value["calendar_native_input"])
                if value.get("calendar_native_input") is not None
                else None,
            )
        except (KeyError, TypeError) as error:
            raise ValueError("invalid TradingView handoff metadata") from error


def export_handoff(
    source: Path,
    output: Path,
    *,
    day: date,
    since: datetime,
    master_input: InputSnapshot | None = None,
    calendar_input: InputSnapshot | None = None,
) -> TradingViewHandoff:
    """Retain new MCP receipts and any exact-date master received in this acquisition.

    The output has no L2 facts, source coverage, or origin from the working copy.
    The publisher must hydrate fresh canonical state and replay these receipts.
    """
    if output.exists():
        raise FileExistsError(output)
    if since.tzinfo is None:
        raise ValueError("TradingView handoff cutoff requires a timezone")
    cutoff = since.astimezone(UTC).isoformat(timespec="microseconds")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        validate_current_schema(conn)
        conn.execute("BEGIN")
        basis, _ = calendar_context(conn, day)
        symbols = tuple(universe(conn, basis))
        receipts = tuple(
            row[0]
            for row in conn.execute(
                "SELECT receipt_id FROM tradingview_source_receipts "
                "WHERE snapshot_date = ? AND received_at_utc >= ? "
                "ORDER BY received_at_utc, receipt_id",
                (day.isoformat(), cutoff),
            )
        )
        if master_input is not None:
            master_input = InputSnapshot.model_validate_json(master_input.model_dump_json())
            if (
                master_input.descriptor is None
                or master_input.descriptor.get("format") != "jquants-master-input-v1"
                or master_input.descriptor.get("as_of") != basis.isoformat()
            ):
                raise ValueError("TradingView master input differs from its required date")
        if calendar_input is not None:
            from baibai_jobs.integration.calendar_native import saved_interval

            calendar_input = InputSnapshot.model_validate_json(calendar_input.model_dump_json())
            if (
                calendar_input.descriptor is None
                or calendar_input.descriptor.get("as_of") != day.isoformat()
                or saved_interval(calendar_input) != (basis, day)
            ):
                raise ValueError("TradingView calendar input differs from its required basis")
        masters = (
            ()
            if master_input is not None
            else tuple(
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT request_id FROM jquants_source_pages "
                    "WHERE source = 'master' AND received_at_utc >= ? ORDER BY request_id",
                    (cutoff,),
                )
            )
        )
        with closing(open_connection(output)) as target, target:
            for table, column, identities in (
                ("tradingview_source_receipts", "receipt_id", receipts),
                ("jquants_source_pages", "request_id", masters),
            ):
                for identity in identities:
                    # All identifiers are fixed in the receipt-only selection above.
                    rows = conn.execute(f"SELECT * FROM {table} WHERE {column} = ?", (identity,))  # nosec B608
                    placeholders = ",".join("?" for _ in rows.description or ())
                    target.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)  # nosec B608
    return TradingViewHandoff(day, basis, symbols, receipts, masters, master_input, calendar_input)


def _copy_raw(
    conn: sqlite3.Connection, source: sqlite3.Connection, identities: Sequence[str]
) -> int:
    inserted = 0
    for identity in identities:
        rows = source.execute(
            "SELECT * FROM tradingview_source_receipts WHERE receipt_id = ?", (identity,)
        )
        names = tuple(item[0] for item in rows.description or ())
        for row in rows:
            existing = conn.execute(
                "SELECT * FROM tradingview_source_receipts WHERE receipt_id = ?", (identity,)
            ).fetchone()
            if existing is not None:
                if tuple(existing) != tuple(row):
                    raise SourceDataError("TradingView handoff receipt identity conflicts")
                continue
            placeholders = ",".join("?" for _ in names)
            conn.execute(f"INSERT INTO tradingview_source_receipts VALUES ({placeholders})", row)  # nosec B608
            inserted += 1
    return inserted


def import_handoff(
    source: Path,
    target: Path,
    handoff: TradingViewHandoff,
    *,
    store: ObjectStore | None = None,
    master_fingerprints: Mapping[str, str] | None = None,
) -> ReplayResult:
    """Add original receipts and replay missing tickers; never replace a saved snapshot.

    The caller materializes its fixed native calendar/master first. Legacy
    master pages are checked against retained cutover fingerprints and never
    copied or projected from the handoff.
    """
    saved_calendar = None
    if handoff.calendar_native_input is not None:
        if store is None:
            raise SourceDataError("TradingView native calendar requires its fixed native context")
        from baibai_jobs.acquisition.calendar_dependencies import saved_calendar_context

        saved_calendar = saved_calendar_context(
            store, handoff.calendar_native_input, observation_date=handoff.snapshot_date
        )
        if saved_calendar[0] != handoff.master_asof:
            raise SourceDataError("TradingView handoff differs from its saved native calendar")
    if handoff.master_native_input is not None:
        if store is None or handoff.master_request_ids:
            raise SourceDataError(
                "TradingView native master handoff requires its fixed native context"
            )
        from baibai_jobs.acquisition.master_dependencies import saved_master_dependency

        request = read_dependency(
            saved_master_dependency(store, handoff.master_native_input, day=handoff.master_asof)
        )
        snapshot = validate_master_snapshot(request.records(), handoff.master_asof)
        if (
            tuple(symbols_from_master_payloads([row[2] for row in snapshot.rows]))
            != handoff.symbols
        ):
            raise SourceDataError(
                "TradingView handoff universe differs from its saved native master"
            )
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as raw,
        closing(open_connection(target)) as conn,
    ):
        validate_current_schema(raw)
        basis, business = calendar_context(conn, handoff.snapshot_date)
        if saved_calendar is not None and saved_calendar != (basis, business):
            raise SourceDataError("TradingView saved calendar differs from canonical calendar")
        if basis != handoff.master_asof:
            raise SourceDataError("TradingView handoff differs from canonical calendar")
        has_master = (
            conn.execute(
                "SELECT 1 FROM jquants_master_snapshots WHERE snapshot_date = ? LIMIT 1",
                (basis.isoformat(),),
            ).fetchone()
            is not None
        )
        if has_master and tuple(universe(conn, basis)) != handoff.symbols:
            raise SourceDataError("TradingView handoff universe differs from canonical master")
        if not has_master:
            raise SourceDataError("TradingView handoff requires one complete exact-date master")
        if handoff.master_request_ids:
            if master_fingerprints is None:
                raise ValueError("legacy TradingView master requires native cutover fingerprints")
            if verify_migrated_master_receipts(
                raw, handoff.master_request_ids, master_fingerprints
            ) != set(handoff.master_request_ids):
                raise ValueError(
                    "legacy TradingView master identity differs from the native cutover"
                )
        for request_id in handoff.master_request_ids:
            request = read_complete_request(raw, request_id)
            if request.source != "master" or not dependency_covers_day(
                "master", request.params, basis
            ):
                raise SourceDataError("TradingView handoff has a different master request")
        with conn:
            raw_added = _copy_raw(conn, raw, handoff.receipt_ids)
    before = snapshot_state(target, handoff.snapshot_date)
    with closing(open_connection(target)) as conn, conn:
        if tuple(universe(conn, basis)) != handoff.symbols:
            raise SourceDataError("TradingView handoff universe differs from replayed master")
        basis, business = calendar_context(conn, handoff.snapshot_date)
        names = tuple(name for name, _, _, _ in COLUMNS)
        sql = (
            f"INSERT OR IGNORE INTO {TABLE} ({','.join(names)}) "  # nosec B608
            f"VALUES ({','.join('?' for _ in names)})"
        )
        added = 0
        rejected = []
        for receipt in read_tool_results(conn, handoff.receipt_ids):
            if (
                receipt.snapshot_date != handoff.snapshot_date
                or receipt.master_asof != basis
                or receipt.tool_name not in BATCH_TOOL_IDENTIFIERS
                or receipt.requested_columns != tuple(observations_owner.COLUMNS)
                or not 1 <= len(receipt.requested_symbols) <= 50
                or len(set(receipt.requested_symbols)) != len(receipt.requested_symbols)
                or not set(receipt.requested_symbols) <= set(handoff.symbols)
            ):
                raise SourceDataError(
                    "TradingView handoff receipt has a different observation context"
                )
            try:
                validate_time(
                    handoff.snapshot_date, receipt.received_at_utc, is_business_day=business
                )
                rows = observations_owner.normalize_batch(
                    decode_tool_result(receipt.result_json),
                    list(receipt.requested_symbols),
                    receipt.received_at_utc,
                )
            except observations_owner.FetchError as error:
                category = failure_category(error)
                if category in {"storage", "internal"}:
                    raise
                rejected.append({"receipt_id": receipt.receipt_id, "reason": category})
                continue
            changes = conn.total_changes
            conn.executemany(
                sql,
                [
                    tuple(
                        handoff.snapshot_date.isoformat()
                        if name == "snapshot_date"
                        else row.get(name)
                        for name in names
                    )
                    for row in rows
                ],
            )
            added += conn.total_changes - changes
    after = snapshot_state(target, handoff.snapshot_date)
    return ReplayResult(
        report={
            **after,
            "rows_added": added,
            "raw_receipts_added": raw_added,
            "rows_before": before.get("rows", 0),
            "rejected_receipts": rejected,
        },
        complete=not rejected,
    )
