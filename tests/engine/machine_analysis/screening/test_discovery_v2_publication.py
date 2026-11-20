"""旧索引と、一部を取り去っても自己整合して見える新索引を全readerで対照する。"""

import gzip
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from tests.engine.machine_analysis.screening.test_discovery_index import fixed

from baibai_engine.investment_judgment.workflow.case_screening import read_case_screening
from baibai_engine.machine_analysis.observations.prices.price_observation import NativePriceSession
from baibai_engine.machine_analysis.screening.discovery_contract import (
    DISCOVERY_V2,
    DiscoveryPolicyV2,
    DiscoverySelectionV2,
    discovery_publication_id,
)
from baibai_engine.machine_analysis.screening.discovery_index import read_discovery_index
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.primitives.json import encode_json


def editions(path):
    meta, _, old, old_id = fixed(path)
    identity = discovery_publication_id(meta.generation_id, DISCOVERY_V2)
    session = NativePriceSession(
        expected_date=meta.as_of, recorded_at=meta.evaluation_time, status="ok"
    )
    rows = tuple(
        {
            **{
                k: v
                for k, v in row.items()
                if k not in {"price", "basis", "price_ready", "price_date", "expected_price_date"}
            },
            "execution_price": {"raw_close_yen": 510.0, "day_state": "mixed"},
        }
        for row in old.result.rows
    )
    new = replace(
        old,
        result=DiscoverySelectionV2(DiscoveryPolicyV2(method=DISCOVERY_V2), rows, {}, session),
        price_inputs=None,
    )
    return meta, old_id, old, identity, new


def test_exact_v1_v2_coexist_unchanged_in_finite_case_and_latest(tmp_path):
    path = tmp_path / "runs"
    _, old_id, old, identity, new = editions(path)
    store = ScreeningSelectionStore(path)
    for key, body in ((old_id, old), (identity, new)):
        assert store.publish(key, body)
    cutoff = datetime(2030, 1, 1, tzinfo=UTC)
    with sqlite3.connect(path) as conn:
        wire = gzip.decompress(
            conn.execute(
                "select payload_gzip from screening_selection where selection_id=?", (old_id,)
            ).fetchone()[0]
        )
    assert (
        sha256(wire).hexdigest()
        == "7f234ba481f9ecd8339dfd57462884d054f3a9d34aff34fc6e5052bb4af9c741"
    )
    original = path.read_bytes()
    for key, source in ((identity, new), (old_id, old)):
        index = read_discovery_index(path=path, selection_id=key, known_at=cutoff)
        assert index.rows == source.result.rows
        assert index.universe_tickers == old.universe_tickers
        target = index.rows[1]["ticker"]
        _, case = read_case_screening(key, path=path, known_at=cutoff, subjects=(target,))
        assert case.result.rows == (index.rows[1],)
        assert case.universe_tickers == index.universe_tickers
        assert case.legacy_candidate_count is None
        if key == identity:
            assert index.price_session == new.result.price_session == case.result.price_session
            assert "price_ready" not in case.result.rows[0]
        else:
            assert case.source_sha256 == sha256(wire).hexdigest()
            assert index.price_session is None
    assert read_discovery_index(path=path, known_at=cutoff).selection_id == identity
    assert path.read_bytes() == original


def test_v2_writer_and_all_exact_readers_compare_against_independent_stock_plan(tmp_path):
    path = tmp_path / "runs"
    _, old_id, old, identity, new = editions(path)
    store = ScreeningSelectionStore(path)
    assert store.publish(old_id, old)
    # Leave a genuine non-first positive and drop one company from BOTH self-reported sets.
    shortened = replace(
        new,
        universe_tickers=new.universe_tickers[:-1],
        result=replace(new.result, rows=new.result.rows[:-1]),
    )
    assert any(
        "profit_growth" in row["matched_paths"]["growth"] for row in shortened.result.rows[1:]
    )
    with pytest.raises(ValueError, match="saved stock plan"):
        store.publish(identity, shortened)
    assert store.publish(identity, new)
    with sqlite3.connect(path) as conn:
        doc = json.loads(
            gzip.decompress(
                conn.execute(
                    "select payload_gzip from screening_selection where selection_id=?", (identity,)
                ).fetchone()[0]
            )
        )
        doc["result"]["rows"].pop()
        doc["universe_tickers"].pop()
        conn.execute(
            "update screening_selection set payload_gzip=? where selection_id=?",
            (gzip.compress(encode_json(doc).encode()), identity),
        )
    cutoff = datetime(2030, 1, 1, tzinfo=UTC)
    for read in (
        lambda: store.read(identity),
        lambda: read_discovery_index(path=path),
        lambda: read_publication(path=path, selection_id=identity),
        lambda: read_case_screening(identity, path=path, known_at=cutoff, subjects=("1234",)),
    ):
        with pytest.raises(ValueError, match="saved stock plan"):
            read()
    assert store.read(old_id) == old


@pytest.mark.parametrize("damage", ["unknown_id", "wrong_body", "future_receipt"])
def test_latest_broken_edition_never_falls_back_to_earlier_v1(tmp_path, damage):
    path = tmp_path / "runs"
    _, old_id, old, identity, new = editions(path)
    store = ScreeningSelectionStore(path)
    assert store.publish(old_id, old)
    assert store.publish(identity, new)
    with sqlite3.connect(path) as conn:
        if damage == "unknown_id":
            bad_id = identity.removesuffix("v2") + "v3"
            conn.execute(
                "update screening_selection set selection_id=? where selection_id=?",
                (bad_id, identity),
            )
        elif damage == "wrong_body":
            conn.execute(
                "update screening_selection set payload_gzip=(select payload_gzip from screening_selection where selection_id=?) where selection_id=?",
                (old_id, identity),
            )
        else:
            doc = json.loads(
                gzip.decompress(
                    conn.execute(
                        "select payload_gzip from screening_selection where selection_id=?",
                        (identity,),
                    ).fetchone()[0]
                )
            )
            doc["result"]["price_session"]["recorded_at"] = "2035-06-26T00:00:00Z"
            conn.execute(
                "update screening_selection set payload_gzip=? where selection_id=?",
                (gzip.compress(encode_json(doc).encode()), identity),
            )
    with pytest.raises(ValueError, match=r"version|identity|validation|cutoff"):
        read_discovery_index(path=path)
    with pytest.raises(ValueError, match=r"version|identity|validation|cutoff"):
        read_publication(path=path)
    assert read_discovery_index(path=path, selection_id=old_id).rows == old.result.rows


def test_raw_scope_changes_only_input_gap_not_the_financial_question_or_valuation(tmp_path):
    from tests.engine.machine_analysis.screening.test_full_fy_fallback import documents, generation

    from baibai_engine.investment_judgment.workflow.discovery import _explore
    from baibai_engine.machine_analysis.screening.policies.discovery import project_discovery_facts

    fixed_models = generation(documents())
    models = tuple(m for m in fixed_models.models if m.subject == "1001")
    row = project_discovery_facts("1001", models=models, metadata=fixed_models)
    # The FA quote is 10, not 510. Only the FA price/share basis can recover this yield.
    earnings = next(p for p in row["coordinates"] if p["path_id"] == "earning_power")
    assert earnings["price_value"] == pytest.approx(150.0 / 800.0)
    assert row["technical_state"] == "unavailable"

    _, _, old, _, new = editions(tmp_path / "runs")
    source_row = next(r for r in new.result.rows if r["ticker"] == "5678")
    old_row = next(r for r in old.result.rows if r["ticker"] == "5678")
    assert source_row["missing"] == []
    assert source_row["technical_state"] == "unavailable"
    for selected, session, gaps in (
        (old_row, None, 1),
        (source_row, new.result.price_session, 0),
        (source_row, new.result.price_session.model_copy(update={"status": "partial"}), 1),
    ):
        menu = _explore((selected,), ("5678",), question=None, page=0, price_session=session)
        counts = {q["key"]: q["company_count"] for q in menu["questions"]}
        assert (counts["input_gap"], counts["earning_power"]) == (gaps, 1)


def test_v2_session_clock_cannot_be_fabricated_through_model_copy_or_numeric_wire(tmp_path):
    meta, _, _, key, selection = editions(tmp_path / "runs")
    session = selection.result.price_session
    for invalid in (None, meta.evaluation_time - timedelta(days=5), 0, "0"):
        altered = replace(
            selection,
            result=replace(
                selection.result, price_session=session.model_copy(update={"recorded_at": invalid})
            ),
        )
        with pytest.raises(ValueError, match=r"requires|validation"):
            ScreeningSelectionStore(tmp_path / "runs").publish(key, altered)
