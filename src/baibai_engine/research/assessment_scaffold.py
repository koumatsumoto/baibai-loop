"""割安機会評価の draft 骨格を、canonical store の値から組み立てる。

数値は promoted thesis と proposal から機械で導出し、判断の散文だけを記入欄として
残す。publish が同じ導出をやり直して照合するので、ここで埋まった数値を手で書き換え
ても保存されない。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.paths import database_path
from baibai_engine.appdb.read import connect_read_only

from .assessment import (
    AssessmentConflictError,
    LaneMachineValues,
    derive_lane_machine_values,
)
from .thesis import ThesisDocument, require_recorded_identity

_PROSE_PLACEHOLDER = "TODO"

# review 前であることを表す番兵。実 hash と衝突しないので publish は必ず落ちる。
# 期待値は `assessment-publish --check` が印字する。
UNREVIEWED_DRAFT_SHA256 = "0" * 64


def scaffold_assessment(
    *,
    db_path: Path | None,
    assessment_id: str,
    as_of: date,
    shortlist_id: str,
    thesis_ids: list[str],
    proposal_id: str | None,
    published_at: datetime,
) -> dict[str, object]:
    """記入欄付きの draft payload を返す。書き出しは呼び出し側が行う。"""
    if not thesis_ids:
        raise AssessmentConflictError("at least one promoted thesis is required")
    path = database_path(db_path)
    if not path.is_file():
        raise AssessmentConflictError(f"application database is unavailable: {path}")
    with closing(connect_read_only(path)) as connection:
        lanes = [_lane_skeleton(connection, thesis_id) for thesis_id in thesis_ids]
        purchase = None if proposal_id is None else _purchase_skeleton(connection, proposal_id)
        shortlist = _shortlist_row(connection, shortlist_id)
    questions = _research_questions_by_ticker(shortlist)
    for lane in lanes:
        lane["research_questions"] = [
            {
                "question": questions.get(str(lane["ticker"]), _PROSE_PLACEHOLDER),
                "answer": _PROSE_PLACEHOLDER,
                "status": "unresolved",
            }
        ]
    macro_context_id = shortlist.get("macro_context_id")
    return {
        "schema_version": 2,
        "kind": "bargain_assessment",
        "assessment_id": assessment_id,
        "as_of": as_of.isoformat(),
        "published_at": published_at.isoformat(),
        "result": "proposal" if proposal_id is not None else "no_actionable_bargain",
        "headline": _PROSE_PLACEHOLDER,
        "shortlist_id": shortlist_id,
        "macro_context_id": macro_context_id if isinstance(macro_context_id, str) else None,
        "comparison": _PROSE_PLACEHOLDER,
        "entry_timing": _PROSE_PLACEHOLDER if proposal_id is not None else None,
        "forgone": _PROSE_PLACEHOLDER,
        "lanes": lanes,
        "purchase": purchase,
        "review": {
            "attempt": 1,
            "reviewer_identity": _PROSE_PLACEHOLDER,
            "reviewed_at": published_at.isoformat(),
            "conclusion": "pass",
            "draft_sha256": UNREVIEWED_DRAFT_SHA256,
            "open_findings": [],
        },
    }


def _research_questions_by_ticker(shortlist: dict[str, object]) -> dict[str, str]:
    """shortlist narrative の `research` 確認事項を ticker ごとに引く。

    候補が深掘りの枠を得た理由そのものなので、draft の記入欄へ先に置いて
    黙って落ちないようにする。
    """
    entries = shortlist.get("entries")
    if not isinstance(entries, list):
        return {}
    questions: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        narrative = entry.get("narrative")
        if not isinstance(narrative, dict):
            continue
        research = narrative.get("research")
        if isinstance(research, str) and research:
            questions[str(entry.get("ticker"))] = research
    return questions


def _lane_skeleton(connection: sqlite3.Connection, thesis_id: str) -> dict[str, object]:
    row = _fetch(
        connection,
        "SELECT ticker, core_sha256, payload FROM thesis WHERE thesis_id = ?",
        (thesis_id,),
    )
    if row is None:
        raise AssessmentConflictError(f"thesis is unavailable: {thesis_id}")
    document = ThesisDocument.model_validate(json.loads(str(row[2])))
    machine = derive_lane_machine_values(document)
    return {
        "ticker": str(row[0]),
        "name": document.input_snapshot.company_name,
        "disposition": _PROSE_PLACEHOLDER,
        "disposition_reason": _PROSE_PLACEHOLDER,
        "reject_class": _PROSE_PLACEHOLDER,
        "thesis_id": thesis_id,
        "thesis_core_sha256": require_recorded_identity(row[1], thesis_id),
        "review_id": None,
        "machine": _machine_payload(machine),
        "business_model": _PROSE_PLACEHOLDER,
        "value_capture": _PROSE_PLACEHOLDER,
        "growth_quality": _PROSE_PLACEHOLDER,
        "financial_resilience": _PROSE_PLACEHOLDER,
        "strongest_countercase": _PROSE_PLACEHOLDER,
        "catalyst": _PROSE_PLACEHOLDER,
        "unknowns": [],
        "source_caveats": [],
    }


def _machine_payload(machine: LaneMachineValues) -> dict[str, object]:
    return machine.model_dump(mode="json")


def _purchase_skeleton(connection: sqlite3.Connection, proposal_id: str) -> dict[str, object]:
    row = _fetch(
        connection,
        "SELECT ticker, payload FROM proposal WHERE proposal_id = ?",
        (proposal_id,),
    )
    if row is None:
        raise AssessmentConflictError(f"proposal is unavailable: {proposal_id}")
    payload = json.loads(str(row[1]))
    planned = payload.get("planned_limit")
    if not isinstance(planned, dict):
        raise AssessmentConflictError(f"proposal {proposal_id} carries no planned limit")
    return {
        "proposal_id": proposal_id,
        "proposal_sha256": sha256(canonical_json(payload).encode("utf-8")).hexdigest(),
        "ticker": str(row[0]),
        "limit_price_yen": planned.get("limit_price_yen"),
        "quantity": planned.get("quantity"),
        "notional_yen": planned.get("notional_yen"),
        "max_acceptable_price_yen": planned.get("max_acceptable_price_yen"),
        "close_yen": planned.get("close_yen"),
        "price_as_of": planned.get("price_as_of"),
        "expires_at": planned.get("expires_at"),
        "warnings": list(planned.get("warnings") or []),
    }


def _shortlist_row(connection: sqlite3.Connection, shortlist_id: str) -> dict[str, object]:
    row = _fetch(
        connection,
        "SELECT payload FROM shortlist WHERE shortlist_id = ?",
        (shortlist_id,),
    )
    if row is None:
        raise AssessmentConflictError(f"shortlist is unavailable: {shortlist_id}")
    payload = json.loads(str(row[0]))
    if not isinstance(payload, dict):
        raise AssessmentConflictError(f"shortlist payload is not an object: {shortlist_id}")
    return payload


def _fetch(
    connection: sqlite3.Connection, sql: str, parameters: tuple[str, ...]
) -> sqlite3.Row | None:
    row: sqlite3.Row | None = connection.execute(sql, parameters).fetchone()
    return row


__all__ = ["UNREVIEWED_DRAFT_SHA256", "scaffold_assessment"]
