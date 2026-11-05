"""固定L1だけからEDINET metricsの比較用候補を再生成する。"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from baibai_engine.data_collection.providers.edinet.archive_native import FixedEdinetArchives
from baibai_engine.data_collection.providers.edinet.list_native import FixedEdinetLists
from baibai_engine.machine_analysis.observations.financials.edinet_integration import (
    extract_saved_edinet,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.revision import (
    EDINET_METRIC_REVISION,
)
from baibai_engine.storage.filesystem import fsync_directory
from baibai_jobs.integration.edinet_list_native import materialize_lists


def rebuild_fixed_edinet_metrics(
    lists: FixedEdinetLists,
    archives: FixedEdinetArchives,
    output: Path,
    *,
    asof: date,
    lookback_days: int = 540,
) -> dict[str, object]:
    """Produce a new candidate, never read an old market or replace its snapshot.

    Fresh list projection supplies retained origins and eligibility evidence.
    Only selected CSV ZIPs are read from the fixed owner; no ZIPs, previous L2,
    facts scope or extraction-success history enter the candidate database.
    """
    if lookback_days < 0:
        raise ValueError("EDINET metric lookback must not be negative")
    if output.exists() or output.is_symlink():
        raise FileExistsError("EDINET metric rebuild requires a new output path")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="edinet-metrics-", dir=output.parent) as temporary:
        database = Path(temporary) / "candidate.sqlite"
        list_report = materialize_lists(lists, database)
        metrics = extract_saved_edinet(
            database, asof=asof, lookback_days=lookback_days, metric_archives=archives
        )["edinet-metrics"]
        output_written = metrics["status"] in {"completed", "partial"}
        if output_written:
            os.link(database, output)
            fsync_directory(output.parent)
    return {
        "list_identity": lists.identity,
        "archive_identity": archives.identity,
        "requested_asof": asof.isoformat(),
        "lookback_days": lookback_days,
        "extractor_revision": EDINET_METRIC_REVISION,
        "output_written": output_written,
        "edinet-list": list_report,
        "edinet-metrics": metrics,
    }
