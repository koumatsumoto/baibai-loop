"""macro調査へregistry・履歴・readingを既存read ownerから見せる。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Any, cast

from baibai_engine.macro.indicators.definitions import load_definitions
from baibai_engine.macro.indicators.read_contracts import point_in_time_providers
from baibai_engine.read_api.macro import stored_macro_reading, stored_macro_rows

from .reader import OwnerError
from .resource_types import Page, Paths, Record, record


def series_page(
    paths: Paths, filters: dict[str, Any], after: list[str | int | float] | None, limit: int
) -> Page:
    rows = sorted((asdict(s) for s in load_definitions().series), key=lambda row: row["series_id"])
    for name, value in filters.items():
        if name != "series_id" and value not in {row[name] for row in rows}:
            raise OwnerError("INVALID_ARGUMENT", f"{name}のregistry値を確認してください。")
    rows = [
        row
        for row in rows
        if all(row.get(k) == v for k, v in filters.items())
        and (after is None or row["series_id"] > after[0])
    ]
    return Page([record(row, ("series_id",), ("series_id",)) for row in rows[:limit]])


def series_get(paths: Paths, selector: dict[str, Any]) -> Page:
    return series_page(paths, selector, None, 1)


def observation_page(
    paths: Paths, filters: dict[str, Any], after: list[str | int | float] | None, limit: int
) -> Page:
    rows = stored_macro_rows(
        paths.macro, kind="observations", filters=filters, after=after, limit=limit
    )
    definition = load_definitions().by_id().get(filters["series_id"])
    meta = {
        "mode": filters.get("mode", "effective"),
        "vintage_basis": "publication"
        if definition and definition.provider in point_in_time_providers()
        else "retrieval",
    }
    return Page(
        [
            record(row, ("series_id", "observed_at", "vintage_at"), ("observed_at", "vintage_at"))
            for row in rows
        ],
        meta,
    )


def observation_get(paths: Paths, selector: dict[str, Any]) -> Page:
    rows = stored_macro_rows(
        paths.macro, kind="observations", filters=selector, after=None, limit=1, exact=True
    )
    return Page([record(row, ("series_id", "observed_at", "vintage_at"), ()) for row in rows])


def provider_page(
    paths: Paths, filters: dict[str, Any], after: list[str | int | float] | None, limit: int
) -> Page:
    rows = stored_macro_rows(
        paths.macro, kind="provider_runs", filters=filters, after=after, limit=limit
    )
    return Page([record(row, ("run_id",), ("page_time", "run_id")) for row in rows])


def provider_get(paths: Paths, selector: dict[str, Any]) -> Page:
    return provider_page(paths, selector, None, 1)


def reading_get(paths: Paths, selector: dict[str, Any]) -> Page:
    snapshot = cast(
        dict[str, Any] | None,
        stored_macro_reading(paths.macro, asof=date.fromisoformat(selector["as_of"])),
    )
    if snapshot is None:
        raise FileNotFoundError("macro store unavailable")
    if "rules_revision" in selector and selector["rules_revision"] != snapshot["rules_revision"]:
        raise OwnerError("REFERENCE_MISMATCH")
    identity = {"as_of": selector["as_of"], "rules_revision": snapshot["rules_revision"]}
    payload = snapshot
    if "series_id" in selector:
        identity["series_id"] = selector["series_id"]
        selected = [row for row in snapshot["series"] if row["series_id"] == selector["series_id"]]
        if not selected:
            raise FileNotFoundError("series unavailable")
        payload = selected[0]
    return Page(
        [Record(payload, identity)],
        {
            "basis": "recomputed",
            "as_of": selector["as_of"],
            "payload_scope": "reading",
            "consistency": "per_call",
        },
    )


def reading_page(
    paths: Paths, filters: dict[str, Any], after: list[str | int | float] | None, limit: int
) -> Page:
    result = reading_get(paths, filters)
    parent = result.records[0]
    rows = [parent.payload] if "series_id" in filters else parent.payload["series"]
    rows = sorted(
        (row for row in rows if after is None or row["series_id"] > after[0]),
        key=lambda row: row["series_id"],
    )
    return Page(
        [
            Record(row, {**parent.identity, "series_id": row["series_id"]}, [row["series_id"]])
            for row in rows[:limit]
        ],
        result.meta,
    )
