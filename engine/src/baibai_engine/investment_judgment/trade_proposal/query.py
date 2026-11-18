"""不変の提案と訂正・人間報告を読む。現在の入力や適用資格は再評価しない。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from copy import deepcopy
from datetime import date, datetime

from baibai_engine.investment_judgment.stock_assessment.store import (
    ReviewedAssessment,
    require_assessment_ref,
)
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    ProposalConflictError,
    TradeProposal,
    proposal_draft_sha256,
)
from baibai_engine.investment_judgment.trade_proposal.record_contracts import (
    HumanProposalReport,
    ProposalCorrection,
    ProposalRecords,
    ProposalReference,
)
from baibai_engine.investment_judgment.workflow.selection import (
    saved_market_assessment,
    selection_from_artifacts,
    selection_sha256,
)
from baibai_engine.investment_judgment.workflow.selection_evidence import (
    read_selection_evidence_from_connection,
)


def has_published_proposal_for_operation(connection: sqlite3.Connection, operation_id: str) -> bool:
    """Operationへの参照束縛前も含め、公開済み提案の存在だけを確認する。"""
    return (
        connection.execute(
            "SELECT 1 FROM investment_proposal "
            "WHERE json_extract(payload, '$.research_scope.operation_id') = ? LIMIT 1",
            (operation_id,),
        ).fetchone()
        is not None
    )


def load_investment_proposal(connection: sqlite3.Connection, proposal_id: str) -> TradeProposal:
    return _load_investment_proposal(connection, proposal_id, None)


def load_proposal_with_assessments(
    connection: sqlite3.Connection, proposal_id: str
) -> tuple[TradeProposal, dict[str, ReviewedAssessment]]:
    """保存表示・振り返りには同じ読取で検証した評価pairも渡す。"""
    pairs: dict[str, ReviewedAssessment] = {}
    return _load_investment_proposal(connection, proposal_id, pairs), pairs


def _load_investment_proposal(
    connection: sqlite3.Connection, proposal_id: str, pairs: dict[str, ReviewedAssessment] | None
) -> TradeProposal:
    """本文/hash/index/正確な評価pairを検証する。現在の資格やprogressは読まない。"""

    row = connection.execute(
        "SELECT as_of,prepared_at,published_at,request_scope,payload,draft_sha256 "
        "FROM investment_proposal WHERE proposal_id=?",
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise FileNotFoundError(f"investment proposal is unavailable: {proposal_id}")
    proposal = TradeProposal.model_validate_json(str(row[4]))
    draft_hash = proposal_draft_sha256(proposal)
    if (
        proposal.proposal_id != proposal_id
        or proposal.as_of.isoformat() != str(row[0])
        or proposal.prepared_at != datetime.fromisoformat(str(row[1]))
        or proposal.published_at != datetime.fromisoformat(str(row[2]))
        or proposal.request_scope != str(row[3])
        or draft_hash != str(row[5])
        or draft_hash != proposal.review.draft_sha256
    ):
        raise ProposalConflictError("stored proposal identity/hash differs from its payload")
    actual_items = connection.execute(
        "SELECT item_index,ticker,action,assessment_id FROM investment_proposal_item "
        "WHERE proposal_id=? ORDER BY item_index",
        (proposal_id,),
    ).fetchall()
    expected_items = [
        (
            index,
            item.ticker,
            item.action,
            None if item.assessment_ref is None else item.assessment_ref.assessment_id,
        )
        for index, item in enumerate(proposal.items)
    ]
    if [tuple(item) for item in actual_items] != expected_items:
        raise ProposalConflictError("stored proposal item index differs from its payload")
    for item in proposal.items:
        if item.assessment_ref is not None:
            pair = require_assessment_ref(connection, item.assessment_ref)
            if pair.document.input_snapshot.ticker != item.ticker:
                raise ProposalConflictError("stored proposal assessment ticker differs")
            if pairs is not None:
                pairs[pair.assessment_id] = pair
    for position in proposal.inputs.positions:
        if position.original_assessment_ref is not None:
            pair = require_assessment_ref(connection, position.original_assessment_ref)
            if pair.document.input_snapshot.ticker != position.ticker:
                raise ProposalConflictError("stored original assessment ticker differs")
            if pairs is not None:
                pairs[pair.assessment_id] = pair
    binding = connection.execute(
        "SELECT reference,ticker,side,origin,proposal_id,source_id,source_digest "
        "FROM position_decision_reference WHERE proposal_id=?",
        (proposal_id,),
    ).fetchall()
    expected_bindings = [
        (proposal_id, item.ticker, "buy", "proposal", proposal_id, proposal_id, draft_hash)
        for item in proposal.items
        if item.action == "buy" and item.terms is not None
    ]
    if [tuple(item) for item in binding] != expected_bindings:
        raise ProposalConflictError("stored proposal buy binding differs from its concrete item")
    return proposal


def published_proposal_ids(connection: sqlite3.Connection) -> tuple[str, ...]:
    """List immutable publications, including corrected originals, for exact history reads."""
    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT proposal_id FROM investment_proposal ORDER BY proposal_id"
        )
    )


def load_latest_investment_proposal(connection: sqlite3.Connection) -> TradeProposal | None:
    proposal_id = ProposalRecordReader(connection).latest_id()
    return None if proposal_id is None else load_investment_proposal(connection, proposal_id)


def _require_ref(connection: sqlite3.Connection, reference: ProposalReference) -> TradeProposal:
    proposal = load_investment_proposal(connection, reference.proposal_id)
    if proposal.review.draft_sha256 != reference.draft_sha256:
        raise ProposalConflictError("proposal record exact reference differs")
    return proposal


def _rank(proposal: TradeProposal) -> tuple[date, datetime, str]:
    return proposal.as_of, proposal.published_at, proposal.proposal_id


def _read_corrections(connection: sqlite3.Connection) -> Iterator[ProposalCorrection]:
    rows = connection.execute(
        "SELECT correction_id,erroneous_proposal_id,replacement_proposal_id,recorded_at,payload "
        "FROM proposal_correction ORDER BY correction_id"
    )
    for row in rows:
        record = ProposalCorrection.model_validate_json(row[4])
        if (
            record.correction_id != row[0]
            or record.erroneous_ref.proposal_id != row[1]
            or record.replacement_ref.proposal_id != row[2]
            or record.recorded_at != datetime.fromisoformat(row[3])
        ):
            raise ProposalConflictError("stored proposal correction header differs")
        erroneous = _require_ref(connection, record.erroneous_ref)
        replacement = _require_ref(connection, record.replacement_ref)
        if _rank(replacement) >= _rank(erroneous) or record.recorded_at < erroneous.published_at:
            raise ProposalConflictError("correction must reference an earlier saved proposal")
        yield record


class ProposalRecordReader:
    """同じ読取transaction内でのみ、検証済みの訂正を対象提案ごとに再利用する。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.by_erroneous_id: dict[str, ProposalCorrection] = {}
        self._by_replacement: dict[str, list[ProposalCorrection]] = {}
        for record in _read_corrections(connection):
            erroneous = record.erroneous_ref.proposal_id
            if erroneous in self.by_erroneous_id:
                raise ProposalConflictError("duplicate or chained proposal correction")
            self.by_erroneous_id[erroneous] = record
            self._by_replacement.setdefault(record.replacement_ref.proposal_id, []).append(record)
        if not self.by_erroneous_id.keys().isdisjoint(self._by_replacement):
            raise ProposalConflictError("duplicate or chained proposal correction")

    def latest_id(self) -> str | None:
        """全scopeから最大の(as_of, aware published_at, ID)を選ぶ。本文破損で旧版へ戻さない。"""

        # Remove only erroneous records from chronological selection. A correction does
        # not pin its replacement over a later, unrelated valid proposal.
        rows = self._connection.execute(
            "SELECT proposal_id,published_at,as_of FROM investment_proposal"
        ).fetchall()
        rows = [row for row in rows if row[0] not in self.by_erroneous_id]
        latest_day = max((row[2] for row in rows), default=None)
        rows = [row for row in rows if row[2] == latest_day]
        headers: list[tuple[datetime, str]] = []
        for row in rows:
            try:
                instant = datetime.fromisoformat(str(row[1]))
                if instant.utcoffset() is None:
                    raise ValueError("stored publication time must include a timezone")
            except ValueError as error:
                raise ProposalConflictError("invalid stored proposal publication header") from error
            headers.append((instant, str(row[0])))
        # Compare aware instants in Python: textual offsets and SQLite julianday
        # respectively lose instant ordering and sub-millisecond precision.
        return max(headers)[1] if headers else None

    def read(
        self, proposal: TradeProposal
    ) -> tuple[list[ProposalCorrection], list[HumanProposalReport]]:
        """同一transactionで検証済みの提案ID・hash・時点に人間報告を照合する。"""
        proposal_id = proposal.proposal_id
        reports = []
        for row in self._connection.execute(
            "SELECT report_id,recorded_at,payload FROM proposal_human_report WHERE proposal_id=?",
            (proposal_id,),
        ):
            report = HumanProposalReport.model_validate_json(row[2])
            if (
                report.report_id != row[0]
                or report.proposal_ref.proposal_id != proposal_id
                or report.proposal_ref.draft_sha256 != proposal.review.draft_sha256
                or report.recorded_at != datetime.fromisoformat(row[1])
                or not proposal.published_at <= report.reported_at <= report.recorded_at
            ):
                raise ProposalConflictError("stored human report identity or time differs")
            reports.append(report)
        reports.sort(key=lambda report: (report.recorded_at, report.report_id))
        correction = self.by_erroneous_id.get(proposal_id)
        return (
            [correction] if correction is not None else self._by_replacement.get(proposal_id, [])
        ), reports


def proposal_records(connection: sqlite3.Connection, proposal: TradeProposal) -> ProposalRecords:
    corrections, reports = ProposalRecordReader(connection).read(proposal)
    return {
        "corrections": [record.model_dump(mode="json") for record in corrections],
        "human_reports": [report.model_dump(mode="json") for report in reports],
    }


def saved_proposal_market_context(
    connection: sqlite3.Connection, proposal: TradeProposal
) -> dict[str, object]:
    scope = proposal.research_scope
    result: dict[str, object] = {
        "source": "proposal_fixed_selection",
        "selection_sha256": scope.selection_sha256,
        "status": "unavailable",
        "assessment": None,
    }
    # This optional historical evidence must not reapply active/current-progress admission.
    row = connection.execute(
        "SELECT payload FROM operation_session WHERE operation_id=?", (scope.operation_id,)
    ).fetchone()
    if row is None:
        return {**result, "reason": "operation_not_found"}
    try:
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise ValueError("unreadable saved artifacts")
        artifacts = payload.get("artifacts", [])
        if not isinstance(artifacts, list) or not all(isinstance(item, dict) for item in artifacts):
            raise ValueError("unreadable saved artifacts")
        selection = selection_from_artifacts(artifacts)
    except (ValueError, TypeError):
        return {**result, "reason": "selection_unreadable"}
    if selection is None:
        return {**result, "reason": "selection_not_recorded"}
    if selection_sha256(selection) == scope.selection_sha256:
        assessment = saved_market_assessment(selection)
    else:
        try:
            evidence = read_selection_evidence_from_connection(
                connection,
                operation_id=scope.operation_id,
                sha256=scope.selection_sha256,
                proposal_id=proposal.proposal_id,
            )
            # Only quote the saved market fragment; old model IDs/order remain opaque.
            original = json.loads(evidence["canonical_json"])
            if (
                evidence["role"] != "as_published"
                or not isinstance(original, dict)
                or original.get("schema_version") not in (3, 4)
                or not isinstance(original.get("inputs"), dict)
            ):
                raise ValueError("unreadable published selection")
            assessment = original["inputs"].get("saved_market_context_assessment")
        except (ValueError, TypeError, KeyError):
            return {**result, "reason": "selection_hash_mismatch"}
    if assessment is None and proposal.market_context_assessment_id is None:
        return {**result, "status": "not_recorded"}
    if (
        not isinstance(assessment, dict)
        or assessment.get("context_id") != proposal.market_context_assessment_id
    ):
        return {**result, "reason": "market_context_reference_mismatch"}
    return {**result, "status": "retained", "assessment": deepcopy(assessment)}
