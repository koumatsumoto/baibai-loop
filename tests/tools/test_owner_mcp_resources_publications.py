from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from tests.engine.test_capital_allocation import _payload as assessment_payload
from tests.engine.test_position_outcome_store import _publication as outcome_publication
from tests.helpers.macro_context import macro_context_payload
from tests.helpers.research_v4 import pair_payload
from tests.tools.test_owner_mcp import reader as reader
from tests.tools.test_owner_mcp_resources import data as data
from tests.tools.test_owner_mcp_resources import error, pages

from baibai_engine.appdb.json import canonical_json
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.macro.context.models import MACRO_CONTEXT_SCHEMA_VERSION, MacroContextDocument
from baibai_engine.macro.context.service import MacroContextService
from baibai_engine.position.outcome_store import PortfolioOutcomeStore


def test_publication_originals_preserve_unknown_fields_without_related_eligibility(data):
    thesis, review = pair_payload()
    thesis["saved_extension"] = {"unrecognized": "preserved"}
    context = MacroContextDocument.model_validate(macro_context_payload())
    MacroContextService(data.paths.application).publish(context, expected_head=None)
    assessment = assessment_payload()
    position = {
        "schema_version": 3,
        "position_review_id": "pr-a",
        "thesis_id": "thesis-a",
        "position_id": "position-a",
        "ticker": "1234",
        "as_of": "2026-09-07",
        "holding": {"quantity": 100, "cost_yen": "100000", "quantity_basis_confirmed": True},
        "quote": None,
        "remaining_reward": {"status": "uncertain", "reason": "未確定"},
        "action": None,
        "unresolved_reason": "追加確認",
    }
    operation = {"checkpoint": "start", "artifacts": [], "canonical_refs": []}
    with sqlite3.connect(data.paths.application) as con:
        con.execute(
            "INSERT INTO thesis VALUES(?,?,?,?,?,?,?,?)",
            (
                "thesis-a",
                "1234",
                "2026-09-07",
                "candidate",
                "2026-09-07T18:00:00+09:00",
                None,
                json.dumps(thesis),
                "a" * 64,
            ),
        )
        con.execute(
            "INSERT INTO thesis_review VALUES(?,?,?,?)",
            (review["review_id"], "thesis-a", review["reviewed_at"], json.dumps(review)),
        )
        con.execute(
            "INSERT INTO capital_allocation_assessment VALUES(?,?,?,?,?,?)",
            (
                assessment["capital_allocation_assessment_id"],
                "2026-08-30",
                "2026-08-30T12:00:00Z",
                "allocate",
                "missing-related-triage",
                canonical_json(assessment),
            ),
        )
        con.execute(
            "INSERT INTO position_review VALUES(?,?,?,?,?,?)",
            ("pr-a", "1234", "2026-09-07", "thesis-a", None, json.dumps(position)),
        )
        con.execute(
            "INSERT INTO operation_session VALUES(?,?,?,?,?,?,?,?)",
            (
                "op-20260907-capital-allocation-1",
                "capital-allocation",
                "active",
                "2026-09-07",
                None,
                "2026-09-07T18:00:00+09:00",
                None,
                json.dumps(operation),
            ),
        )
    outcome = outcome_publication()
    PortfolioOutcomeStore(data.paths.application).publish(outcome)
    cases = [
        ("macro.context", {"context_id": context.context_id}, context.model_dump(mode="json")),
        ("research.thesis", {"thesis_id": "thesis-a"}, thesis),
        ("research.thesis_review", {"review_id": review["review_id"]}, review),
        (
            "research.capital_allocation_assessment",
            {"capital_allocation_assessment_id": assessment["capital_allocation_assessment_id"]},
            json.loads(canonical_json(assessment)),
        ),
        ("position.review", {"position_review_id": "pr-a"}, position),
        ("operation.session", {"operation_id": "op-20260907-capital-allocation-1"}, operation),
        ("portfolio.outcome", {"outcome_id": outcome.outcome_id}, outcome.payload),
    ]
    with sqlite3.connect(data.paths.application) as con:
        before = list(con.iterdump())
    for name, selector, expected in cases:
        result = data.get(name, selector)
        assert result["payload"]["payload"] == expected
        assert result["meta"]["validation"] == "current"
        assert data.get(name, resource_ref=result["resource_ref"])["payload"] == result["payload"]
        assert pages(data, name)
    with sqlite3.connect(data.paths.application) as con:
        assert list(con.iterdump()) == before
    latest = data.get("macro.context", {"as_of": context.as_of.isoformat()})
    assert latest["meta"]["head_context_id"] == context.context_id
    error(lambda: data.get("macro.context", {"latest": 1}), "INVALID_ARGUMENT")


def test_old_publication_is_stored_only_but_current_corruption_is_error(data):
    with sqlite3.connect(data.paths.application) as con:
        con.execute(
            "INSERT INTO macro_context VALUES(?,?,?,?,?,?)",
            (
                "old",
                1,
                "2026-01-01",
                "2026-01-01T00:00:00Z",
                None,
                json.dumps({"schema_version": 1, "context_id": "old", "old_field": "preserve"}),
            ),
        )
        con.execute(
            "INSERT INTO macro_context VALUES(?,?,?,?,?,?)",
            (
                "bad",
                MACRO_CONTEXT_SCHEMA_VERSION,
                "2026-01-02",
                "2026-01-02T00:00:00Z",
                None,
                json.dumps(
                    {
                        "schema_version": MACRO_CONTEXT_SCHEMA_VERSION,
                        "context_id": "bad",
                    }
                ),
            ),
        )
    assert data.get("macro.context", {"context_id": "old"})["meta"]["validation"] == "stored_only"
    assert len(pages(data, "macro.context")) == 2
    error(lambda: data.get("macro.context", {"context_id": "bad"}), "CONTRACT_MISMATCH")


def test_er_saved_payload_survives_expiry_and_meta_does_not_change_ref(data, monkeypatch):
    import baibai_engine.read_api.er_calibration_context as module

    raw = safe_load(Path("reports/published/er-level-calibration-latest.yaml").read_text())
    data.paths.er.write_text(yaml.safe_dump(raw))
    monkeypatch.setattr(
        module, "er_calibration_unavailable_reason", lambda *_args, **_kwargs: "expired"
    )
    expired = data.get("screening.er_calibration_context", {})
    assert expired["payload"] == raw
    assert expired["meta"]["unavailable_reason"] == "expired"
    monkeypatch.setattr(
        module,
        "er_calibration_unavailable_reason",
        lambda *_args, **_kwargs: "rules_identity_mismatch",
    )
    mismatch = data.get("screening.er_calibration_context", resource_ref=expired["resource_ref"])
    assert mismatch["resource_ref"] == expired["resource_ref"]
    data.paths.er.write_text("schema_version: invalid")
    error(lambda: data.get("screening.er_calibration_context", {}), "CONTRACT_MISMATCH")
    data.paths.er.unlink()
    error(lambda: data.get("screening.er_calibration_context", {}), "SOURCE_UNAVAILABLE")


def test_wire_values_hash_and_oversized_get(data):
    from hashlib import sha256

    from tools.owner_mcp.resource_types import Page, Record

    spec = data.specs["portfolio.ledger"]
    payload = {"decimal": Decimal("1.23456789"), "day": date(2026, 1, 1), "null": None}
    data.specs[spec.resource_id] = replace(spec, get=lambda *_: Page([Record(payload, {})]))
    got = data.get(spec.resource_id, {})
    assert got["payload"] == {"decimal": "1.23456789", "day": "2026-01-01", "null": None}
    assert (
        got["resource_ref"]["sha256"] == sha256(canonical_json(got["payload"]).encode()).hexdigest()
    )
    payload["text"] = "あ" * 100000
    error(lambda: data.get(spec.resource_id, {}), "RESULT_TOO_LARGE")
    payload.clear()
    payload["bad"] = float("nan")
    error(lambda: data.get(spec.resource_id, {}), "CONTRACT_MISMATCH")


@pytest.mark.parametrize(("field", "value"), [("ticker", "9999"), ("as_of", "2026-09-08")])
@pytest.mark.parametrize("version", [3, 4])
def test_thesis_selected_row_identity_must_match_payload(data, field, value, version):
    thesis, _ = pair_payload()
    thesis["schema_version"] = version
    with sqlite3.connect(data.paths.application) as con:
        con.execute(
            "INSERT INTO thesis VALUES(?,?,?,?,?,?,?,?)",
            (
                "thesis-a",
                "1234",
                "2026-09-07",
                "candidate",
                "2026-09-07T18:00:00Z",
                None,
                json.dumps(thesis),
                "a" * 64,
            ),
        )
    result = data.get("research.thesis", {"thesis_id": "thesis-a"})
    assert result["payload"]["payload"] == thesis
    assert result["meta"]["validation"] == ("current" if version == 4 else "stored_only")
    with sqlite3.connect(data.paths.application) as con:
        con.execute(f"UPDATE thesis SET {field}=?", (value,))
    error(lambda: data.get("research.thesis", {"thesis_id": "thesis-a"}), "CONTRACT_MISMATCH")


def test_context_schema_column_cannot_disguise_payload_as_historical(data):
    old = {"schema_version": 1, "context_id": "old", "as_of": "2026-01-01", "old_field": "raw"}
    with sqlite3.connect(data.paths.application) as con:
        con.execute(
            "INSERT INTO macro_context VALUES(?,?,?,?,?,?)",
            ("old", 1, "2026-01-01", "2026-01-01T00:00:00Z", None, json.dumps(old)),
        )
    result = data.get("macro.context", {"context_id": "old"})
    assert result["payload"]["payload"] == old
    assert result["meta"]["validation"] == "stored_only"
    with sqlite3.connect(data.paths.application) as con:
        con.execute("UPDATE macro_context SET schema_version=?", (MACRO_CONTEXT_SCHEMA_VERSION,))
    error(lambda: data.get("macro.context", {"context_id": "old"}), "CONTRACT_MISMATCH")


def test_review_list_distinguishes_missing_parent_from_empty_and_preserves_old(data):
    filters = {"thesis_id": "old-thesis"}
    error(lambda: data.list("research.thesis_review", filters), "SOURCE_UNAVAILABLE")
    old = {"schema_version": 3, "saved_extension": "not re-evaluated"}
    review = {"review_id": "old-review", "old_field": "raw"}
    with sqlite3.connect(data.paths.application) as con:
        con.execute(
            "INSERT INTO thesis VALUES(?,?,?,?,?,?,?,?)",
            (
                "old-thesis",
                "1234",
                "2026-01-01",
                "candidate",
                "2026-01-01T00:00:00Z",
                None,
                json.dumps(old),
                "a" * 64,
            ),
        )
    assert data.list("research.thesis_review", filters)["items"] == []
    with sqlite3.connect(data.paths.application) as con:
        con.execute(
            "INSERT INTO thesis_review VALUES(?,?,?,?)",
            ("old-review", "old-thesis", "2026-01-01T00:00:00Z", json.dumps(review)),
        )
    listed = data.list("research.thesis_review", filters)
    result = data.get("research.thesis_review", listed["items"][0]["selector"])
    assert result["payload"]["payload"] == review
    assert result["meta"]["validation"] == "stored_only"
