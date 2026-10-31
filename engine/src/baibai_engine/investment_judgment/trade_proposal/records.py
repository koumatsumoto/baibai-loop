"""提案本文を変えない追記サービス。取引ledgerは更新しない。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path

from baibai_engine.investment_judgment.trade_proposal.contracts import (
    ProposalConflictError,
    proposal_draft_sha256,
)
from baibai_engine.investment_judgment.trade_proposal.query import (
    _require_ref,
    load_corrections,
    load_investment_proposal,
    load_latest_investment_proposal,
    proposal_records,
)
from baibai_engine.investment_judgment.trade_proposal.record_contracts import (
    HumanProposalReport,
    HumanProposalReportDraft,
    ProposalCorrection,
    ProposalCorrectionDraft,
    ProposalRecords,
    ProposalReference,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.primitives.time import JST
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_engine.storage.sqlite.application.write import connect_rw, initialize_database


class ProposalRecordService:
    def __init__(
        self, db_path: Path | None = None, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._db_path = db_path
        self._clock = clock or (lambda: datetime.now(JST))

    def correct(
        self, draft: ProposalCorrectionDraft, *, expected_latest_ref: ProposalReference
    ) -> ProposalCorrection:
        draft = ProposalCorrectionDraft.model_validate(draft.model_dump())
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT payload FROM proposal_correction WHERE correction_id=?",
                    (draft.correction_id,),
                ).fetchone()
                if existing is not None:
                    saved = next(
                        r
                        for r in load_corrections(connection)
                        if r.correction_id == draft.correction_id
                    )
                    if saved.model_dump(exclude={"recorded_at"}) != draft.model_dump():
                        raise ProposalConflictError("immutable proposal correction differs")
                    connection.rollback()
                    return saved
                latest = load_latest_investment_proposal(connection)
                if (
                    latest is None
                    or ProposalReference(
                        proposal_id=latest.proposal_id, draft_sha256=proposal_draft_sha256(latest)
                    )
                    != expected_latest_ref
                ):
                    raise ProposalConflictError("proposal latest changed; reload before correction")
                record = ProposalCorrection(**draft.model_dump(), recorded_at=self._clock())
                connection.execute(
                    "INSERT INTO proposal_correction VALUES (?,?,?,?,?)",
                    (
                        record.correction_id,
                        record.erroneous_ref.proposal_id,
                        record.replacement_ref.proposal_id,
                        record.recorded_at.isoformat(),
                        canonical_json(record.model_dump(mode="json")),
                    ),
                )
                load_corrections(connection)
                connection.commit()
                return record
            except BaseException:
                connection.rollback()
                raise

    def report(self, draft: HumanProposalReportDraft) -> HumanProposalReport:
        draft = HumanProposalReportDraft.model_validate(draft.model_dump())
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                proposal = _require_ref(connection, draft.proposal_ref)
                existing = connection.execute(
                    "SELECT payload FROM proposal_human_report WHERE report_id=?",
                    (draft.report_id,),
                ).fetchone()
                if existing is not None:
                    stored = HumanProposalReport.model_validate_json(existing[0])
                    if stored.proposal_ref != draft.proposal_ref:
                        raise ProposalConflictError("immutable human proposal report differs")
                    records = proposal_records(connection, stored.proposal_ref.proposal_id)
                    saved = next(
                        HumanProposalReport.model_validate_json(canonical_json(r))
                        for r in records["human_reports"]
                        if r["report_id"] == draft.report_id
                    )
                    if saved.model_dump(exclude={"recorded_at"}) != draft.model_dump():
                        raise ProposalConflictError("immutable human proposal report differs")
                    connection.rollback()
                    return saved
                record = HumanProposalReport(**draft.model_dump(), recorded_at=self._clock())
                if not proposal.published_at <= record.reported_at <= record.recorded_at:
                    raise ProposalConflictError(
                        "human report time precedes proposal or follows receipt"
                    )
                connection.execute(
                    "INSERT INTO proposal_human_report VALUES (?,?,?,?)",
                    (
                        record.report_id,
                        record.proposal_ref.proposal_id,
                        record.recorded_at.isoformat(),
                        canonical_json(record.model_dump(mode="json")),
                    ),
                )
                connection.commit()
                return record
            except BaseException:
                connection.rollback()
                raise

    def read(self, proposal_id: str) -> ProposalRecords:
        with closing(connect_read_only(self._db_path)) as connection:
            connection.execute("BEGIN")
            load_investment_proposal(connection, proposal_id)
            return proposal_records(connection, proposal_id)
