"""固定一覧とsource scopeからZIPを取得し、L2を待たずに原本を確定する。"""

from __future__ import annotations

import io
import time
import zipfile
from datetime import date, timedelta
from typing import Literal, Protocol

from baibai_engine.data_collection.providers.edinet.archive_input import read_archive_list_input
from baibai_engine.data_collection.providers.edinet.archive_native import (
    EdinetArchiveL1,
    NativeArchive,
)
from baibai_engine.data_collection.providers.edinet.archive_plan import (
    needs_archive,
    prepare_fact_acquisition,
    select_holder_archives,
    select_metric_archives,
)
from baibai_engine.data_collection.providers.edinet.facts_scope import (
    FactsScope,
)
from baibai_engine.data_collection.providers.edinet.list_native import FixedEdinetLists
from baibai_engine.data_collection.providers.edinet.models import (
    EdinetAccessDeniedError,
    EdinetAcquisitionError,
    EDINETProviderError,
    EdinetRateLimitError,
    EdinetResponse,
)
from baibai_engine.data_collection.transport.http import AcquisitionDeadlineExceeded
from baibai_engine.storage.json import write_json_atomic

# Retained IDs form the queue; distribute this backfill across normal daily runs.
_HOLDER_ARCHIVES_PER_RUN = 100
_HOLDER_INTERVAL_SECONDS = 10


class ArchiveClient(Protocol):
    def fetch_archive(self, doc_id: str, source_type: int) -> EdinetResponse: ...


def acquire_archives(
    owner: EdinetArchiveL1,
    client: ArchiveClient,
    lists: FixedEdinetLists,
    *,
    group: Literal["edinet-metrics", "edinet-facts"],
    asof: date,
    lookback_days: int = 540,
    initialize_facts: bool = False,
    tickers: tuple[str, ...] = (),
    include_holder_archives: bool = True,
) -> dict[str, object]:
    if group not in {"edinet-metrics", "edinet-facts"}:
        raise ValueError("unsupported EDINET archive group")
    if lookback_days < 0:
        raise ValueError("EDINET lookback must not be negative")
    if group == "edinet-metrics" and (initialize_facts or tickers):
        raise ValueError("annual initialization and ticker selection require edinet-facts")
    owner.require_clean_work()
    fixed = owner.freeze()
    owner.directory.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        owner.directory / "input.json",
        {
            "group": group,
            "asof": asof.isoformat(),
            "lookback_days": lookback_days,
            "initialize_facts": initialize_facts,
            "tickers": list(tickers),
            "include_holder_archives": include_holder_archives,
            "list_head": lists.value(),
            "archive_head": fixed.value(),
        },
    )
    retained = set(fixed.archive_keys)
    holder_ids: set[str] = set()
    holder_planning_failed = False
    selected_ids: frozenset[str] = frozenset()
    report: dict[str, object] = {
        "source": group,
        "asof": asof.isoformat(),
        "list_identity": lists.identity,
    }
    inputs = read_archive_list_input(lists)
    report["list_failed_days"] = [day.isoformat() for day in inputs.failed_days]
    documents: list[tuple[str, Literal[1, 5]]]
    if group == "edinet-metrics":
        start = asof - timedelta(days=lookback_days)
        inputs.require_complete_window(start, asof)
        candidates, quarantined = select_metric_archives(
            inputs.observed_documents(through=asof), origin_start=start
        )
        documents = [(doc_id, 5) for doc_id in candidates]
    else:
        plan = prepare_fact_acquisition(
            inputs.stored_documents(through=asof)
            if fixed.scope.since is not None or initialize_facts
            else [],
            fixed.scope,
            initialize=initialize_facts,
            tickers=tickers,
        )
        selected = plan.documents
        selected_ids = frozenset(str(doc["docID"]) for doc in (selected or []))
        if plan.scope != fixed.scope:
            owner.publish((), scope=plan.scope)
        documents = [
            (str(doc["docID"]), 1) for doc in (selected or []) if needs_archive(doc, asof=asof)
        ]
        holder_pending: list[str] = []
        quarantined = 0
        if include_holder_archives:
            try:
                holder_pending, quarantined = select_holder_archives(
                    inputs.observed_documents(through=asof, holder_only=True), retained=retained
                )
            except (EDINETProviderError, ValueError, OSError):
                holder_planning_failed = True
        holder_ids = set(holder_pending[:_HOLDER_ARCHIVES_PER_RUN])
        documents.extend((doc_id, 1) for doc_id in holder_pending[:_HOLDER_ARCHIVES_PER_RUN])
        report.update(
            annual_archive_plan="available" if selected is not None else "skipped_not_initialized",
            holder_planning_failed=holder_planning_failed,
            holder_not_selected_this_batch=max(0, len(holder_pending) - len(holder_ids)),
        )
    failed: list[dict[str, str]] = []
    annual_failed = False
    downloaded = reused = 0
    for doc_id, source_type in documents:
        if (doc_id, source_type) in retained:
            reused += 1
            continue
        try:
            response = client.fetch_archive(doc_id, source_type)
            if not zipfile.is_zipfile(io.BytesIO(response.body)):
                raise ValueError("EDINET archive response is not a ZIP")
        except (EdinetAcquisitionError, AcquisitionDeadlineExceeded, ValueError, OSError) as error:
            failed.append({"doc_id": doc_id, "reason": type(error).__name__})
            annual_failed |= doc_id in selected_ids
            if isinstance(
                error, (AcquisitionDeadlineExceeded, EdinetRateLimitError, EdinetAccessDeniedError)
            ):
                break
        else:
            # 保存失敗は取得失敗に丸めずpendingを残す。次のHTTPより先に確定する。
            owner.publish([NativeArchive.from_response(doc_id, source_type, response)])
            retained.add((doc_id, source_type))
            downloaded += 1
        if doc_id in holder_ids:
            time.sleep(_HOLDER_INTERVAL_SECONDS)
    if initialize_facts and not tickers and selected_ids and not annual_failed:
        owner.publish((), scope=FactsScope(since=asof))
    report.update(
        status="failed" if failed or holder_planning_failed or inputs.failed_days else "received",
        downloaded=downloaded,
        retained_archives_reused=reused,
        failed=failed,
        document_events_quarantined=quarantined,
        l1_identity=owner.freeze().identity,
    )
    return report
