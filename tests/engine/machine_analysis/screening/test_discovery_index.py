from __future__ import annotations

import gzip
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest
from tests.engine.machine_analysis.screening.test_technical_financial import evaluate_fixed
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
    legacy = evaluate_fixed(full)
    subjects = legacy.universe_tickers
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
