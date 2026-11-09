from contextlib import closing

import pytest
from tests.helpers.jquants_receipts import (
    FIRST_AT,
    calendar,
)

from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration.jquants_handoff import (
    MARKET_SOURCES,
    export_handoff,
    import_handoff,
    validate_source_handoff,
)


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
    source, target, stage = (tmp_path / name for name in ("source", "target", "stage"))
    receipt = calendar(source, "2026-10-01", "2026-10-10", 0, FIRST_AT)
    with closing(open_connection(target)):
        pass
    selected = export_handoff(source, stage, since=FIRST_AT, sources=MARKET_SOURCES)
    assert selected == (receipt,)
    original, before = stage.read_bytes(), target.read_bytes()
    with pytest.raises(ValueError, match="without native cutover proof"):
        import_handoff(stage, target, selected, sources=MARKET_SOURCES)
    assert target.read_bytes() == before
    assert stage.read_bytes() == original
