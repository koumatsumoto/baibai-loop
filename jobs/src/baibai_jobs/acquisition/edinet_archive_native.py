"""固定一覧とsource scopeからZIPを取得し、L2を待たずに原本を確定する。"""

from __future__ import annotations

import io
import sqlite3
import time
import zipfile
from contextlib import closing
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, Protocol

from baibai_engine.data_collection.providers.edinet.archive_native import (
    EdinetArchiveL1,
    NativeArchive,
)
from baibai_engine.data_collection.providers.edinet.facts_scope import (
    FactsScope,
    merge_scope,
    read_scope,
)
from baibai_engine.data_collection.providers.edinet.list_native import FixedEdinetLists
from baibai_engine.data_collection.providers.edinet.models import (
    EdinetAcquisitionError,
    EdinetRateLimitError,
    EdinetResponse,
)
from baibai_engine.data_collection.transport.http import AcquisitionDeadlineExceeded
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.edinet_archives import (
    _HOLDER_ARCHIVES_PER_RUN,
    _HOLDER_INTERVAL_SECONDS,
    holder_archive_candidates,
    metric_archive_candidates,
)
from baibai_jobs.acquisition.edinet_fact_plan import needs_archive, prepare_fact_acquisition
from baibai_jobs.integration.edinet_list_native import materialize_lists


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
    # 一時DBは固定原本から作るplanner用adapter。旧marketやZIP本文をコピーしない。
    with TemporaryDirectory(prefix="edinet-plan-", dir=owner.directory) as temporary:
        database = Path(temporary) / "lists.sqlite"
        list_report = materialize_lists(lists, database)
        report["list_failed_days"] = list_report["failed_days"]
        with closing(open_connection(database)) as connection, connection:
            merge_scope(connection, fixed.scope)
        documents: list[tuple[str, Literal[1, 5]]]
        if group == "edinet-metrics":
            candidates, quarantined = metric_archive_candidates(
                database, asof=asof, lookback_days=lookback_days
            )
            documents = [(doc_id, 5) for doc_id in candidates]
        else:
            selected = prepare_fact_acquisition(
                database, asof=asof, initialize=initialize_facts, tickers=tickers
            )
            selected_ids = frozenset(str(doc["docID"]) for doc in (selected or []))
            with closing(open_connection(database)) as connection:
                scope = read_scope(connection)
            if scope != fixed.scope:
                owner.publish((), scope=scope)
            documents = [
                (str(doc["docID"]), 1) for doc in (selected or []) if needs_archive(doc, asof=asof)
            ]
            try:
                holder_pending, quarantined = holder_archive_candidates(
                    database, asof=asof, retained=retained
                )
            except (sqlite3.Error, EDINETProviderError, ValueError, OSError):
                holder_pending, quarantined = [], 0
                holder_planning_failed = True
            holder_ids = set(holder_pending[:_HOLDER_ARCHIVES_PER_RUN])
            documents.extend((doc_id, 1) for doc_id in holder_pending[:_HOLDER_ARCHIVES_PER_RUN])
            report.update(
                annual_archive_plan="available"
                if selected is not None
                else "skipped_not_initialized",
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
            if isinstance(error, (AcquisitionDeadlineExceeded, EdinetRateLimitError)) or (
                doc_id in holder_ids
                and isinstance(error, EdinetAcquisitionError)
                and str(error).endswith(("status 401", "status 403"))
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
        status="failed"
        if failed or holder_planning_failed or list_report["failed_days"]
        else "received",
        downloaded=downloaded,
        retained_archives_reused=reused,
        failed=failed,
        document_events_quarantined=quarantined,
        l1_identity=owner.freeze().identity,
    )
    return report
