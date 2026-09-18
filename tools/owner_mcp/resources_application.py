"""資本確認・調査工程へapplication原本を関連objectの再評価なしで見せる。"""

from __future__ import annotations

from typing import Any

from baibai_engine.read_api.position import ledger_rows
from baibai_engine.read_api.publications import TABLES as TABLES
from baibai_engine.read_api.publications import publication_rows

from .resource_types import Page, Paths, record


def publication_page(
    paths: Paths,
    filters: dict[str, Any],
    after: list[str | int | float] | None,
    limit: int,
    *,
    table: str,
    full: bool = False,
) -> Page:
    rows, meta = publication_rows(
        paths.application, table=table, filters=filters, after=after, limit=limit, full=full
    )
    identifier, sort, _ = TABLES[table]
    sort = tuple(
        "page_time" if key in {"published_at", "reviewed_at", "started_at"} else key for key in sort
    )
    return Page([record(row, (identifier,), sort) for row in rows], meta)


def publication_get(paths: Paths, selector: dict[str, Any], *, table: str) -> Page:
    return publication_page(paths, selector, None, 1, table=table, full=True)


def ledger_page(
    paths: Paths,
    filters: dict[str, Any],
    after: list[str | int | float] | None,
    limit: int,
    *,
    kind: str,
) -> Page:
    rows = ledger_rows(paths.application, kind=kind, filters=filters, after=after, limit=limit)
    identity = {
        "ledger_meta": (),
        "ledger_event": ("event_id",),
        "ledger_market_price": ("ticker",),
    }[kind]
    sort = {"ledger_meta": (), "ledger_event": ("append_seq",), "ledger_market_price": ("ticker",)}[
        kind
    ]
    return Page(
        [record(row, identity, sort) for row in rows],
        {
            "payload_scope": "ledger_header" if kind == "ledger_meta" else "row",
            "semantics": "broker_fact" if kind != "ledger_market_price" else "stored_price",
            **(
                {"children": ["portfolio.ledger_event", "portfolio.market_price"]}
                if kind == "ledger_meta"
                else {}
            ),
        },
    )


def ledger_get(paths: Paths, selector: dict[str, Any], *, kind: str) -> Page:
    return ledger_page(paths, selector, None, 1, kind=kind)
