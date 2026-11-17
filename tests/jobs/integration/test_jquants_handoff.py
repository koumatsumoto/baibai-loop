import json
import sqlite3
from contextlib import closing

import pytest
from tests.engine.data_collection.providers.jquants.test_calendar_native import attempt
from tests.helpers.jquants_receipts import (
    FIRST_AT,
    calendar,
    receive_page,
)
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.calendar_native import CalendarL1
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration.jquants_handoff import (
    MARKET_SOURCES,
    export_handoff,
    import_handoff,
    validate_source_handoff,
)
from baibai_jobs.integration.source_handoff import _replay


def test_retired_financial_handoff_is_rejected_before_opening_a_store(tmp_path):
    with pytest.raises(ValueError, match="unsupported J-Quants handoff sources"):
        export_handoff(
            tmp_path / "missing.sqlite",
            tmp_path / "stage.sqlite",
            since=FIRST_AT,
            sources=("financial_summaries",),
        )
    with (
        closing(open_connection(tmp_path / "source.sqlite")) as connection,
        pytest.raises(ValueError, match="unsupported J-Quants handoff sources"),
    ):
        validate_source_handoff(connection, sources=("financial_summaries",))


def test_unproven_legacy_packet_cannot_restore_the_retired_projection_path(tmp_path):
    source, stage = (tmp_path / name for name in ("source", "stage"))
    receipt = calendar(source, "2026-10-01", "2026-10-10", 0, FIRST_AT)
    selected = export_handoff(source, stage, since=FIRST_AT, sources=MARKET_SOURCES)
    assert selected == (receipt,)
    original = stage.read_bytes()
    with pytest.raises(ValueError, match="without native cutover proof"):
        import_handoff(stage, selected, sources=MARKET_SOURCES)
    assert stage.read_bytes() == original


@pytest.mark.parametrize("destination", ["absent", "arbitrary_bytes", "existing_store"])
@pytest.mark.parametrize("proof", ["exact", "missing", "different"])
def test_native_cutover_check_does_not_open_a_projection_store(
    tmp_path, monkeypatch, destination, proof
):
    store = MemoryR2Store()
    owner = CalendarL1(store, tmp_path / "native")
    owner.initialize()
    original = attempt("calendar")
    owner.commit([original])
    fingerprints = {
        key: row["fingerprint"] for key, row in owner.fixed().value()["receipts"].items()
    }
    if proof == "missing":
        fingerprints = {}
    elif proof == "different":
        fingerprints = {"calendar": "f" * 64}
    packet = tmp_path / "packet"
    packet.mkdir()
    receipt = packet / "receipts.sqlite"
    with closing(open_connection(receipt)) as connection, connection:
        receive_page(connection, original.pages[0], request_id="calendar", page_index=0)
    (packet / "result.json").write_text(
        json.dumps({"source": "jquants-market", "request_ids": ["calendar"]})
    )
    target = tmp_path / "unused-market.sqlite"
    if destination == "arbitrary_bytes":
        target.write_bytes(b"not a projection store")
    elif destination == "existing_store":
        with closing(open_connection(target)):
            pass

    def files():
        return {
            str(path.relative_to(tmp_path)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in tmp_path.rglob("*")
            if path.is_file()
        }

    before = files()
    connects = []
    connect = sqlite3.connect

    def observed_connect(database, *args, **kwargs):
        connects.append((str(database), kwargs))
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", observed_connect)

    def check():
        return _replay(
            "jquants-market",
            target,
            packet,
            master_fingerprints={},
            calendar_fingerprints=fingerprints,
            price_fingerprints={},
            margin_alerts_fingerprints={},
            short_sale_reports_fingerprints={},
            margin_interest_fingerprints={},
        )

    if proof == "exact":
        result = check()
        assert result.complete
        assert result.report["raw_pages_added"] == 0
        assert result.report["migrated_calendar_requests"] == ["calendar"]
        assert result.report["replayed_requests"] == []
        assert result.report["incomplete_requests"] == []
    else:
        with pytest.raises(ValueError, match="finite cutover"):
            check()
    assert connects == [(receipt.resolve().as_uri() + "?mode=ro", {"uri": True})]
    assert files() == before
