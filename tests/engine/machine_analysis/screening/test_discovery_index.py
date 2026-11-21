from __future__ import annotations

import gzip
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256

import pytest
from tests.helpers.archived_technical_financial import (
    SIX_STOCK_ORDER,
    stored_publication,
    stored_row,
)
from tests.helpers.technical_financial import _seed

from baibai_engine.machine_analysis.generation.contracts import GenerationMetadata
from baibai_engine.machine_analysis.screening import discovery_index
from baibai_engine.machine_analysis.screening.policies.discovery import (
    evaluate_discovery_rows,
    project_discovery_row,
)
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.primitives.json import encode_json


def fixed(path):
    full = _seed(path)
    meta = GenerationMetadata.model_validate(full.model_dump(exclude={"models"}))
    # A historical publication is a declared input, never reconstructed from current models.
    growth = ["profit_growth", "operating_recovery"]
    recorded = [
        ("1234", "growth_research", "matched", "research_candidate", growth),
        ("2345", "no_current_hypothesis", "matched", "not_selected", []),
        ("3456", "input_review", "matched", "input_unresolved", []),
        ("4567", "growth_research", "unavailable", "research_candidate", growth),
        ("5678", "growth_research", "unavailable", "research_candidate", growth),
        ("6789", "input_review", "matched", "input_unresolved", []),
    ]
    legacy = stored_publication(
        meta.generation_id,
        [
            stored_row(
                t, lane=lane, technical={"state": ta}, gate={"state": g, "matched_routes": routes}
            )
            for t, lane, ta, g, routes in recorded
        ],
        order=SIX_STOCK_ORDER,
    )
    subjects = tuple(sorted(t for t, plan in meta.subject_plan.items() if plan.scope == "stock"))
    rows = {
        t: project_discovery_row(
            t, models=tuple(m for m in full.models if m.subject == t), metadata=meta
        )
        for t in subjects
    }
    compact = evaluate_discovery_rows(meta, tickers=subjects, rows=rows)
    identity = f"selection-{meta.generation_id}-opportunity-discovery-v1"
    return meta, legacy, compact, identity


def test_publication_scope_clocks_values_and_old_body_is_never_opened(tmp_path, monkeypatch):
    path = tmp_path / "runs"
    assert discovery_index.read_discovery_index(path=path).status == "no_publication"
    meta, legacy, compact, identity = fixed(path)
    store = ScreeningSelectionStore(path)
    assert store.publish("historic", legacy)
    with monkeypatch.context() as m:
        m.setattr(gzip, "decompress", lambda *_: pytest.fail("legacy lookup opened the full body"))
        none = discovery_index.read_discovery_index(path=path, selection_id="unknown")
        assert none.status == "no_publication"
        old = discovery_index.read_discovery_index(path=path, selection_id="historic")
        assert (old.status, old.selection_id, old.generation_id, old.rows) == (
            "unindexed_legacy",
            "historic",
            meta.generation_id,
            (),
        )
    assert store.publish(identity, compact)
    assert not store.publish(identity, compact)
    with sqlite3.connect(path) as conn:
        (wire,) = conn.execute(
            "SELECT payload_gzip FROM screening_selection WHERE selection_id=?", (identity,)
        ).fetchone()
    assert sha256(gzip.decompress(wire)).hexdigest() == (
        "7f234ba481f9ecd8339dfd57462884d054f3a9d34aff34fc6e5052bb4af9c741"
    )
    normal = discovery_index.read_discovery_index(path=path)
    assert normal.status == "indexed"
    assert normal.selection_id == identity
    assert normal.universe_tickers == legacy.universe_tickers
    for r, old in zip(normal.rows, legacy.result.rows, strict=True):
        assert (r["ticker"], r["screening_lane"], r["technical_state"], r["financial_state"]) == (
            old.ticker,
            old.screening_lane,
            old.technical["state"],
            old.financial_gate["state"],
        )
        assert r["matched_paths"] == {
            "financial": old.financial_paths["matched_routes"],
            "growth": old.financial_gate["matched_routes"],
        }
        assert r["coordinates"] == [
            {
                k: v[k]
                for k in (
                    "path_id",
                    "variant",
                    "price_group",
                    "business_group",
                    "price_value",
                    "business_value",
                )
            }
            for v in old.financial_paths["paths"]
            if v["eligible"]
        ]
        assert {"technical", "financial_gate", "financial_paths", "evidence"}.isdisjoint(r)
    assert (
        discovery_index.read_discovery_index(
            path=path, selection_id=identity, known_at=normal.published_at
        ).rows
        == normal.rows
    )
    assert (
        discovery_index.read_discovery_index(
            path=path,
            selection_id=identity,
            known_at=normal.published_at - timedelta(microseconds=1),
        ).status
        == "no_publication"
    )
    assert (
        discovery_index.read_discovery_index(
            path=path, selection_id=identity, as_of=meta.as_of - timedelta(days=1)
        ).status
        == "no_publication"
    )
    with pytest.raises(ValueError, match="offset"):
        discovery_index.read_discovery_index(
            path=path, known_at=meta.evaluation_time.replace(tzinfo=None)
        )
    assert store.read("historic") == legacy


def test_missing_source_is_an_explicit_member_not_an_absent_or_duplicate_row(tmp_path):
    path = tmp_path / "runs"
    meta, _, compact, identity = fixed(path)
    t = compact.universe_tickers[1]
    sparse = evaluate_discovery_rows(
        meta,
        tickers=compact.universe_tickers,
        rows={r["ticker"]: r for r in compact.result.rows if r["ticker"] != t},
    )
    missing = next(r for r in sparse.result.rows if r["ticker"] == t)
    assert missing["models_recorded"] is False
    assert missing["coordinates"] == []
    assert missing["price"] is None
    assert missing["capital_action"] is None
    store = ScreeningSelectionStore(path)
    assert store.publish(identity, sparse)
    with pytest.raises(ValueError, match="different content"):
        store.publish(identity, compact)
    broken = replace(
        sparse,
        result=replace(sparse.result, rows=sparse.result.rows[:1] * len(sparse.universe_tickers)),
    )
    with pytest.raises(ValueError, match="exact stock universe"):
        store.publish(identity, broken)
    with pytest.raises(ValueError, match="identity"):
        store.publish("wrong", sparse)
    with pytest.raises(ValueError, match="version or identity"):
        store.publish(identity.removesuffix("v1") + "unknown", sparse)
    with sqlite3.connect(path) as c:
        blob = c.execute(
            "select payload_gzip from screening_selection where selection_id=?", (identity,)
        ).fetchone()[0]
        doc = json.loads(gzip.decompress(blob))
        doc["result"]["rows"].pop(1)
        c.execute(
            "update screening_selection set payload_gzip=? where selection_id=?",
            (gzip.compress(encode_json(doc).encode()), identity),
        )
    with pytest.raises(ValueError, match="differs"):
        discovery_index.read_discovery_index(path=path, selection_id=identity)


@pytest.mark.parametrize(
    ("field", "partial"),
    [
        ("matched_paths", {}),
        ("coordinates", [{"path_id": "earning_power"}]),
        ("capital_action", {"observations": 0}),
    ],
)
def test_writer_rejects_incomplete_evidence_instead_of_publishing_empty_meaning(
    tmp_path, field, partial
):
    path = tmp_path / "runs"
    _, _, source, identity = fixed(path)
    only = source.result.rows[0]
    altered = replace(
        source,
        result=replace(source.result, rows=({**only, field: partial}, *source.result.rows[1:])),
    )
    with pytest.raises(ValueError, match="Field required"):
        ScreeningSelectionStore(path).publish(identity, altered)
    assert discovery_index.read_discovery_index(path=path).status == "no_publication"


@pytest.mark.parametrize("bad", [True, False, "0.50", "NaN", float("nan"), float("inf")])
def test_non_numbers_and_nonfinite_amounts_never_become_observed_value(tmp_path, bad):
    from pydantic import TypeAdapter, ValidationError

    from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection

    path = tmp_path / "runs"
    _, _, source, identity = fixed(path)
    coord = {
        "path_id": "earning_power",
        "variant": "annual_ttm",
        "price_group": "synthetic_price",
        "business_group": "synthetic_business",
        "price_value": 0.5,
        "business_value": 4,
    }
    first = {**source.result.rows[0], "coordinates": [coord]}
    source = replace(source, result=replace(source.result, rows=(first, *source.result.rows[1:])))
    store = ScreeningSelectionStore(path)
    assert store.publish(identity, source)
    with sqlite3.connect(path) as c:
        original = json.loads(
            gzip.decompress(c.execute("select payload_gzip from screening_selection").fetchone()[0])
        )
        selected = next(i for i, row in enumerate(original["result"]["rows"]) if row["coordinates"])
        coord = original["result"]["rows"][selected]["coordinates"][0]
        coord["price_value"] = bad
        # Deliberately malformed stored JSON, including non-standard constants from a corrupt source.
        wire = json.dumps(original, allow_nan=True).encode()
        c.execute("update screening_selection set payload_gzip=?", (gzip.compress(wire),))
    with pytest.raises(ValidationError):
        discovery_index.read_discovery_index(path=path)
    with pytest.raises(ValidationError):
        TypeAdapter(ScreeningSelection).validate_python(original)
    row = source.result.rows[selected]
    altered = {**row, "coordinates": [{**row["coordinates"][0], "price_value": bad}]}
    require = replace(
        source,
        result=replace(
            source.result,
            rows=tuple(
                altered if i == selected else old for i, old in enumerate(source.result.rows)
            ),
        ),
    )
    with pytest.raises(ValidationError):
        store.publish(identity, require)
