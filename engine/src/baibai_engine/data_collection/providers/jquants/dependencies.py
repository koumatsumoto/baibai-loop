"""Bounded native master/calendar requests for acquisition dependencies only."""

from __future__ import annotations

import base64
import gzip
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsAcquisitionError,
    JQuantsPage,
)
from baibai_engine.data_collection.providers.jquants.receipts import (
    JQuantsRequest,
    complete_request,
    read_complete_request,
)


def dependency_covers_day(source: str, params: Mapping[str, str], day: date) -> bool:
    """Compare native basic/extended ISO dates without changing the saved query."""
    try:
        if source == "master" and set(params) == {"date"}:
            return date.fromisoformat(params["date"]) == day
        if source == "market_calendar" and set(params) == {"from", "to"}:
            return date.fromisoformat(params["from"]) <= day <= date.fromisoformat(params["to"])
    except ValueError:
        return False
    return False


def export_dependencies(database: Path, *, day: date, master_asof: date) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        for (request_id,) in conn.execute(
            "SELECT DISTINCT request_id FROM jquants_source_pages "
            "WHERE source IN ('master', 'market_calendar')"
        ):
            try:
                request = read_complete_request(conn, request_id)
            except JQuantsAcquisitionError:
                continue
            if not dependency_covers_day(
                request.source, request.params, master_asof if request.source == "master" else day
            ):
                continue
            previous = latest.get(request.source)
            if previous and request.received_at <= datetime.fromisoformat(previous["received_at"]):
                continue
            latest[request.source] = {
                "format": "jquants-acquisition-dependency-v1",
                "request_id": request_id,
                "source": request.source,
                "params": dict(request.params),
                "received_at": request.received_at.isoformat(),
                "pages": [
                    {
                        "query": dict(page.query),
                        "received_at": page.received_at.isoformat(),
                        "body_gzip": base64.b64encode(gzip.compress(page.body, mtime=0)).decode(),
                    }
                    for page in request.pages
                ],
            }
    return list(latest.values())


def read_dependency(value: dict[str, Any]) -> JQuantsRequest:
    if value["format"] != "jquants-acquisition-dependency-v1" or value["source"] not in {
        "master",
        "market_calendar",
    }:
        raise ValueError("unexpected acquisition dependency source")
    request = complete_request(
        tuple(
            JQuantsPage(
                value["source"],
                page["query"],
                datetime.fromisoformat(page["received_at"]),
                gzip.decompress(base64.b64decode(page["body_gzip"], validate=True)),
            )
            for page in value["pages"]
        )
    )
    expected = {"date"} if request.source == "master" else {"from", "to"}
    if (
        set(request.params) != expected
        or dict(request.params) != value["params"]
        or request.received_at != datetime.fromisoformat(value["received_at"])
    ):
        raise ValueError("acquisition dependency differs from its original receipt")
    return request
