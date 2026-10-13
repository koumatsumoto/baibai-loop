"""所有移動の保存互換性とcold import/読取境界を検証する。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.helpers.research_triage import published_review_set, research_triage_payload

from baibai_engine.research.triage import query
from baibai_engine.research.triage.contracts import ResearchTriage, StoredResearchTriage
from baibai_engine.research.triage.service import ResearchTriageService
from baibai_engine.screening.discovery.contracts import PublishedReviewSet
from baibai_engine.storage.application.json import canonical_json


def test_saved_triage_and_review_set_payloads_keep_pre_move_hashes() -> None:
    raw = research_triage_payload(schema_version=3, triage_contract_id="research-triage-v3")
    raw["macro_context_id"] = raw.pop("market_context_assessment_id")
    triage = StoredResearchTriage.from_payload(raw)
    review_set = PublishedReviewSet.model_validate(published_review_set())
    # 移動前main 548373b8の同じ合成fixtureから取得したcanonical hash。
    assert query.research_triage_payload_hash(triage) == (
        "5c0197454f48d03278dbf0ec9720fa688dbed4c834931ea1bb8cc9d04cb3faff"
    )
    assert sha256(canonical_json(review_set.model_dump(mode="json")).encode()).hexdigest() == (
        "c3df397ce7646d1c2280115c8fee6b02faeebd0e858b3ea2300aaa7415850b32"
    )
    quality = triage.entries[0].candidate_snapshot.analysis.data_quality
    payload = quality.model_dump(mode="json")
    assert "ttm_quality_fcf" not in payload
    explicit = quality.model_validate({**payload, "ttm_quality_fcf": None})
    assert "ttm_quality_fcf" in explicit.model_dump(mode="json")


@pytest.mark.parametrize(
    "module",
    [
        "baibai_engine.screening.discovery.contracts",
        "baibai_engine.research.triage.contracts",
        "baibai_engine.research.triage.query",
    ],
)
def test_cold_contract_and_query_imports_do_not_load_generation_or_writers(module: str) -> None:
    script = """
import importlib, json, sys
importlib.import_module(sys.argv[1])
print(json.dumps(sorted(name for name in sys.modules if name.startswith('baibai_engine.'))))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, module], capture_output=True, text=True, check=True
    )
    modules = json.loads(result.stdout)
    forbidden = (
        "baibai_engine.read_api",
        "baibai_engine.storage.application.write",
        "baibai_engine.research.triage.service",
        "baibai_engine.research.triage.cli",
        "baibai_engine.screening.discovery.review_set",
        "baibai_engine.screening.metrics",
        "baibai_engine.screening.run_store",
        "baibai_engine.market",
        "baibai_engine.macro",
        "baibai_engine.research.workspace",
    )
    assert not [name for name in modules if name.startswith(forbidden)]
    if module.endswith("contracts"):
        assert not [name for name in modules if name.startswith("baibai_engine.storage")]


def test_owner_queries_are_read_only_and_absent_store_stays_absent(tmp_path, monkeypatch) -> None:
    path = tmp_path / "application.sqlite"
    assert query.latest_research_triage_id(path) is None
    assert query.list_research_triage_payloads(path) == []
    assert not path.exists()
    triage = ResearchTriage.model_validate(research_triage_payload())
    review_set = PublishedReviewSet.model_validate(
        published_review_set(
            review_set_id=triage.review_set_id,
            run_revision_id=triage.run_revision_id,
            as_of=triage.as_of.isoformat(),
        )
    )
    published = ResearchTriageService(path).publish(triage, review_set=review_set)
    before = path.read_bytes()
    open_read_only = query.connect_read_only
    statements: list[str] = []

    def connect_read_only(selected: Path) -> sqlite3.Connection:
        connection = open_read_only(selected)
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(query, "connect_read_only", connect_read_only)
    stored = query.current_research_triage(path, published.research_triage_id)
    assert stored is not None
    assert stored.publication_payload() == published.model_dump(mode="json")
    assert query.latest_research_triage_id(path) == published.research_triage_id
    assert query.research_triage_payloads_for_review_set(path, published.review_set_id) == [
        published.model_dump(mode="json")
    ]
    assert path.read_bytes() == before
    assert statements
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)


def test_saved_triage_is_readable_with_original_reference_but_cannot_be_republished(tmp_path):
    raw = research_triage_payload(schema_version=3, triage_contract_id="research-triage-v3")
    raw["macro_context_id"] = "macro-context-2026-07-17-saved"
    raw.pop("market_context_assessment_id")
    saved = StoredResearchTriage.from_payload(raw)
    assert saved.schema_version == 3
    assert saved.market_context_assessment_id == raw["macro_context_id"]
    assert saved.publication_payload() == raw
    assert saved.admissible_research_tickers() == ("2331",)
    with pytest.raises(ValidationError):
        ResearchTriage.model_validate(raw)
    database = tmp_path / "not-created.sqlite"
    with pytest.raises(ValueError, match="read-only"):
        ResearchTriageService(database).publish(
            saved, review_set=PublishedReviewSet.model_validate(published_review_set())
        )
    assert not database.exists()
