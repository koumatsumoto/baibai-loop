"""候補調査へ全保存分析と較正結果をread ownerから見せる。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from baibai_engine.read_api.calibration import read_rows
from baibai_engine.read_api.er_calibration_context import stored_er_calibration_context
from baibai_engine.read_api.screening import (
    screening_calibration_method_identity,
    stored_review_set,
    stored_screening_rows,
    stored_screening_run,
)

from .resource_types import Page, Paths, Record, record


def run_page(
    paths: Paths,
    filters: dict[str, Any],
    after: list[str | int | float] | None,
    limit: int,
    *,
    kind: str,
) -> Page:
    rows = stored_screening_rows(paths.runs, kind=kind, filters=filters, after=after, limit=limit)
    identity, sort = {
        "screening_run": (("run_revision_id",), ("asof_date", "page_time", "run_revision_id")),
        "security_analysis": (("run_revision_id", "ticker"), ("ordinal",)),
        "review_set": (("review_set_id",), ("asof_date", "page_time", "review_set_id")),
    }[kind]
    return Page([record(row, identity, sort) for row in rows])


def run_get(paths: Paths, selector: dict[str, Any]) -> Page:
    row = stored_screening_run(paths.runs, selector)
    return Page(
        [record(row, ("run_revision_id",), ())],
        {
            "payload_scope": "run_header",
            "children": [
                {
                    "resource_id": "screening.security_analysis",
                    "filters": {"run_revision_id": row["run_revision_id"]},
                }
            ],
        },
    )


def analysis_get(paths: Paths, selector: dict[str, Any]) -> Page:
    return run_page(paths, selector, None, 1, kind="security_analysis")


def review_get(paths: Paths, selector: dict[str, Any]) -> Page:
    args = {
        ("as_of_date" if key == "as_of" else key): value
        for key, value in selector.items()
        if key != "latest"
    }
    publication = stored_review_set(paths.runs, **args)
    if publication is None:
        raise FileNotFoundError("review set unavailable")
    return Page(
        [
            Record(
                {
                    "review_set_id": publication.review_set_id,
                    "run_revision_id": publication.run_revision_id,
                    "as_of": publication.as_of,
                    "created_at": publication.created_at,
                    "payload": publication.payload,
                },
                {"review_set_id": publication.review_set_id},
            )
        ]
    )


def calibration_page(
    paths: Paths,
    filters: dict[str, Any],
    after: list[str | int | float] | None,
    limit: int,
    *,
    kind: str,
) -> Page:
    rows, token = read_rows(paths.calibration, kind=kind, filters=filters, after=after, limit=limit)
    identity, sort = {
        "cohort": (("asof",), ("asof",)),
        "panel_row": (("asof", "ticker"), ("ticker",)),
        "forward_row": (("asof", "ticker", "horizon"), ("ticker", "horizon")),
    }[kind]
    return Page([record(row, identity, sort) for row in rows], {"snapshot_token": token})


def calibration_get(paths: Paths, selector: dict[str, Any], *, kind: str) -> Page:
    return calibration_page(paths, selector, None, 1, kind=kind)


def er_get(paths: Paths, selector: dict[str, Any]) -> Page:
    identity = screening_calibration_method_identity(Path())
    if identity is None:
        raise ValueError("production identity unavailable")
    payload, reason = stored_er_calibration_context(
        paths.er,
        expected_rules_hash=identity[0],
        expected_er_model_version=identity[1],
        as_of=datetime.now(ZoneInfo("Asia/Tokyo")).date(),
    )
    return Page([Record(payload, {})], {"usable": reason is None, "unavailable_reason": reason})
