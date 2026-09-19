from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from baibai_engine.foundation.yaml_io import strict_safe_load
from baibai_engine.macro.context import cli, models
from baibai_engine.macro.context.handoff import MacroContextHandoff, load_handoff

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/macro_context_handoff_v1.yaml"


@pytest.fixture
def payload():
    return strict_safe_load(FIXTURE.read_text(encoding="utf-8"))


def _set(payload, path, value):
    node = payload
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), True),
        (("schema_version",), "1"),
        (("schema_version",), 2),
        (("as_of",), 0),
        (("generated_at",), 1789730000),
        (("evidence", 4, "observation_as_of"), "2026-06-01"),
        (("kind",), "macro-context"),
        (("published_at",), "2026-09-18T23:00:00+09:00"),
        (("analysis_id",), "macro-handoff-2026-09-17-example"),
        (("generated_at",), "2026-09-18T23:00:00"),
        (("generated_at",), "2026-09-17T23:00:00+09:00"),
        (("summary",), "   "),
        (("bindings", "limitations"), []),
        (("evidence", 0, "observations", 0, "value"), True),
        (("evidence", 0, "observations", 0, "value"), "4.0"),
        (("evidence", 0, "observations", 0, "value"), float("nan")),
        (("evidence", 0, "observations", 1, "observed_at"), "2026-09-19"),
        (("evidence", 0, "observations", 1, "observed_at"), "2026-06-17"),
        (("evidence", 4, "input_evidence_ids"), ["missing-evidence"]),
        (("evidence", 4, "input_evidence_ids"), ["ev-rate-change"]),
        (("core", 0, "judgment", "source_ids"), ["missing-evidence"]),
        (("core", 0, "previous_scorecard_review"), "not allowed"),
        (("core", 1, "judgment", "direction"), "buy"),
        (("synthesis", "dominant_forces", 0, "core_section_ids"), ["rates_policy"]),
        (("synthesis", "dominant_forces", 0, "series_ids"), ["us.10y"]),
        (("scenarios", 0, "probability"), "0.5"),
        (("scenarios", 0, "probability"), True),
        (("scenarios", 0, "probability"), 0.51),
        (("scenarios", 0, "probability"), 0.55),
        (("scenarios", 0, "scorecard_intents"), []),
        (("scenarios", 0, "scorecard_intents", 0, "series_id"), "unbacked.series"),
        (("monitoring", 0, "machine_conditions"), []),
        (("connection", "bargain_topography", "source_ids"), ["ev-rates"]),
        (("connection", "estimate_caveats"), []),
    ],
)
def test_rejects_broken_contract(payload, path, value):
    _set(payload, path, value)
    with pytest.raises(ValidationError):
        MacroContextHandoff.model_validate(payload)


def test_yaml_json_round_trip_and_scientific_notation(payload, tmp_path):
    document = load_handoff(FIXTURE)
    target = tmp_path / "handoff.json"
    target.write_text(document.model_dump_json(), encoding="utf-8")
    assert load_handoff(target).model_dump(mode="json") == document.model_dump(mode="json")
    payload["evidence"][0]["observations"][0]["value"] = 1e-5
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert load_handoff(target).evidence[0].observations[0].value == 1e-5


def test_core_scenario_order_and_duplicate_ids(payload):
    for key in ("core", "scenarios"):
        changed = deepcopy(payload)
        changed[key].reverse()
        with pytest.raises(ValidationError):
            MacroContextHandoff.model_validate(changed)
    for collection in ("evidence", "external_sources", "data_issues"):
        changed = deepcopy(payload)
        changed[collection].append(deepcopy(changed[collection][0]))
        with pytest.raises(ValidationError, match="duplicate"):
            MacroContextHandoff.model_validate(changed)


def test_unverified_article_may_be_recorded_but_not_cited(payload):
    payload["external_sources"][0].update(status="unverified", limitation="not retrieved")
    with pytest.raises(ValidationError, match="unverified"):
        MacroContextHandoff.model_validate(payload)
    payload["synthesis"]["dominant_forces"][0]["source_ids"].remove("ev-article")
    MacroContextHandoff.model_validate(payload)


def test_exclusion_propagates_through_calculation(payload):
    # No direct source claim names ev-rates after this rewrite; it is still unsafe
    # to cite its numerical result via ev-rate-change.
    payload["data_issues"] = [
        {
            "issue_id": "exclude-rates",
            "severity": "exclude",
            "reason": "wrong unit",
            "affected_series": ["us.10y"],
        }
    ]
    raw = json.dumps(payload).replace(
        '"source_ids": ["ev-rates"', '"source_ids": ["ev-rate-change"'
    )
    changed = json.loads(raw)
    with pytest.raises(ValidationError, match="excluded"):
        MacroContextHandoff.model_validate(changed)


def test_direct_evidence_exclusion_and_warning(payload):
    payload["data_issues"] = [
        {
            "issue_id": "exclude-market",
            "severity": "exclude",
            "reason": "incorrect universe",
            "evidence_ids": ["ev-market"],
        }
    ]
    with pytest.raises(ValidationError, match="excluded"):
        MacroContextHandoff.model_validate(payload)
    payload["data_issues"][0]["severity"] = "warning"
    MacroContextHandoff.model_validate(payload)


def test_unbacked_force_and_connection_are_rejected(payload):
    payload["core"][5]["series_ids"] = ["usd_jpy"]
    force = payload["synthesis"]["dominant_forces"][0]
    force["core_section_ids"] = ["fx", "japan"]
    payload["core"][6]["series_ids"] = ["usd_jpy"]
    with pytest.raises(ValidationError, match="named sections"):
        MacroContextHandoff.model_validate(payload)
    payload["synthesis"]["dominant_forces"][0]["core_section_ids"] = ["rates_policy", "fx"]
    payload["connection"]["core_section_ids"] = ["fx"]
    with pytest.raises(ValidationError, match="named core"):
        MacroContextHandoff.model_validate(payload)


@pytest.mark.parametrize("evidence_index", [0, 2], ids=["series", "market"])
def test_observation_date_cannot_exceed_access_date(payload, evidence_index):
    evidence = payload["evidence"][evidence_index]
    # Both fixtures observe September 17; vintage is deliberately absent.
    evidence["accessed_at"] = "2026-09-16T14:59:59Z"
    with pytest.raises(ValidationError, match="observation date exceeds access date"):
        MacroContextHandoff.model_validate(payload)
    # The same UTC date is September 17 in JST: same-day access is valid.
    evidence["accessed_at"] = "2026-09-16T15:00:00Z"
    MacroContextHandoff.model_validate(payload)


def test_binding_and_source_time_consistency(payload):
    payload["bindings"]["reading"] = {"as_of": "2026-09-19", "rules_revision": "synthetic-example"}
    with pytest.raises(ValidationError, match="reading window"):
        MacroContextHandoff.model_validate(payload)
    payload["bindings"]["reading"]["as_of"] = "2026-09-18"
    payload["external_sources"][0]["published_on"] = "2026-09-19"
    with pytest.raises(ValidationError, match="beyond as_of"):
        MacroContextHandoff.model_validate(payload)
    payload["external_sources"][0]["published_on"] = "2026-09-17"
    payload["evidence"][0]["observations"][0]["vintage_at"] = "2026-09-19T00:00:00+09:00"
    with pytest.raises(ValidationError, match="exceeds generated_at"):
        MacroContextHandoff.model_validate(payload)


@pytest.mark.parametrize(
    ("suffix", "text"),
    [
        (".yaml", "schema_version: 1\nschema_version: 1\n"),
        (".yaml", "a: [unterminated"),
        (".yaml", "---\na: 1\n---\nb: 2\n"),
        (".json", '{"schema_version":1,"schema_version":1}'),
        (".json", '{"value":NaN}'),
        (".json", '{"value":Infinity}'),
        (".json", '{"value":1,}'),
        (".txt", "schema_version: 1"),
    ],
)
def test_cli_reports_parser_errors_without_traceback(tmp_path, capsys, suffix, text):
    artifact = tmp_path / f"bad{suffix}"
    artifact.write_text(text, encoding="utf-8")
    assert cli.main(["handoff", "validate", str(artifact)]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err)["status"] == "error"
    assert "Traceback" not in output.err


def test_commands_never_open_store_or_consult_registry(monkeypatch, capsys, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("handoff must not access stores or registry")

    monkeypatch.setattr(cli, "MacroContextService", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(models, "load_definitions", forbidden)
    assert cli.main(["handoff", "validate", str(FIXTURE), "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["validation_scope"] == "structure_only"
    assert result["publish_ready"] is False
    assert result["input_bindings_complete"] is False
    assert cli.main(["handoff", "schema", "--format", "json"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["HandoffMonitoringPoint"]["additionalProperties"] is False
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    database = tmp_path / "not-created.sqlite"
    assert cli.main(["--db", str(database), "handoff", "schema"]) == 1
    assert not database.exists()
    assert json.loads(capsys.readouterr().err)["status"] == "error"


def test_handoff_is_not_a_publication(payload):
    with pytest.raises(ValidationError):
        models.MacroContextDocument.model_validate(payload)
