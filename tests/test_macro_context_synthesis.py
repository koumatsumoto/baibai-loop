"""Contract tests for the integrated strategy layer of the macro context.

Split by the layer that owns each rule: internal consistency (force references,
series containment, probability arithmetic) lives on the document model and runs on
every load, while presence (synthesis, probabilities, caveats, topography grounding)
is a publication gate so that revisions published before the fields keep loading.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from baibai_engine.appdb.write import initialize_database
from baibai_engine.macro.context.models import (
    MacroContextDocument,
    require_integrated_strategy,
    scorecard_snapshot_input_id,
)
from baibai_engine.macro.context.service import MacroContextService
from baibai_engine.read_api.macro import latest_macro_context_payload
from tests.helpers.macro_context import macro_context_payload

_RISK_SECTION = 8  # index of risk_environment in the fixed core order


def _payload(**kwargs: Any) -> dict[str, Any]:
    return macro_context_payload(**kwargs)


def _set_probabilities(payload: dict[str, Any], values: tuple[float | None, ...]) -> None:
    scenarios = payload["core"][_RISK_SECTION]["scenarios"]
    for scenario, value in zip(scenarios, values, strict=True):
        if value is None:
            scenario.pop("probability", None)
        else:
            scenario["probability"] = value


def _validated(payload: dict[str, Any]) -> MacroContextDocument:
    return MacroContextDocument.model_validate(payload)


# --- model layer: internal consistency runs on every load ---


def test_model_rejects_a_single_dominant_force() -> None:
    payload = _payload()
    payload["synthesis"]["dominant_forces"] = payload["synthesis"]["dominant_forces"][:1]
    payload["synthesis"]["interactions"] = []
    with pytest.raises(ValidationError):
        _validated(payload)


def test_model_rejects_six_dominant_forces() -> None:
    payload = _payload()
    forces = payload["synthesis"]["dominant_forces"]
    while len(forces) < 6:
        clone = json.loads(json.dumps(forces[0]))
        clone["force_id"] = f"clone-{len(forces)}"
        forces.append(clone)
    with pytest.raises(ValidationError):
        _validated(payload)


def test_model_rejects_a_force_confined_to_one_section() -> None:
    payload = _payload()
    payload["synthesis"]["dominant_forces"][0]["core_section_ids"] = ["rates_policy"]
    with pytest.raises(ValidationError):
        _validated(payload)


def test_model_rejects_a_force_naming_a_non_channel_section() -> None:
    payload = _payload()
    payload["synthesis"]["dominant_forces"][0]["core_section_ids"] = [
        "regime_summary",
        "rates_policy",
    ]
    with pytest.raises(ValidationError, match="transmission-channel sections"):
        _validated(payload)


def test_model_rejects_a_force_citing_series_outside_its_named_sections() -> None:
    payload = _payload()
    payload["synthesis"]["dominant_forces"][0]["series_ids"] = ["us.10y", "jp.10y"]
    with pytest.raises(ValidationError, match="series its named sections cite"):
        _validated(payload)


def test_model_rejects_a_force_whose_named_section_contributes_no_series() -> None:
    # Naming a channel that lends none of the force's series would make the
    # cross-channel claim nominal: the force would "cross" into fx on paper only.
    payload = _payload()
    payload["inputs"]["indicator_series"].append(
        {
            "input_id": "jp-10y",
            "provider": "mof_jgb",
            "series_id": "jp.10y",
            "window": "2026-07-01/2026-07-17",
            "observation_as_of": "2026-07-17",
            "published_at": "2026-07-17T16:00:00+09:00",
            "accessed_at": "2026-07-19T12:00:00+09:00",
            "status": "ok",
            "used_for": "日本金利の確認",
        }
    )
    fx_section = payload["core"][5]
    assert fx_section["section_id"] == "fx"
    fx_section["series_ids"] = ["jp.10y"]
    for item in (
        *fx_section["fact_summary"],
        fx_section["judgment"],
        fx_section["economic_connection"],
    ):
        item["source_ids"] = ["jp-10y"]
    with pytest.raises(ValidationError, match="from each named section"):
        _validated(payload)


def test_model_rejects_a_force_series_without_a_cited_successful_input() -> None:
    payload = _payload()
    # The named sections cite the series, but the force itself does not carry the
    # series input in its own source trail.
    payload["synthesis"]["dominant_forces"][0]["source_ids"] = ["reading-2026-07-19"]
    with pytest.raises(ValidationError, match="no cited successful indicator input"):
        _validated(payload)


def test_model_rejects_a_blank_counter_evidence() -> None:
    payload = _payload()
    payload["synthesis"]["dominant_forces"][0]["counter_evidence"] = "  "
    with pytest.raises(ValidationError, match="non-blank"):
        _validated(payload)


def test_model_rejects_duplicate_force_ids() -> None:
    payload = _payload()
    forces = payload["synthesis"]["dominant_forces"]
    forces[1]["force_id"] = forces[0]["force_id"]
    with pytest.raises(ValidationError, match="force_id must be unique"):
        _validated(payload)


def test_model_rejects_a_synthesis_without_interactions() -> None:
    payload = _payload()
    payload["synthesis"]["interactions"] = []
    with pytest.raises(ValidationError):
        _validated(payload)


def test_model_rejects_an_interaction_naming_an_undeclared_force() -> None:
    payload = _payload()
    payload["synthesis"]["interactions"][0]["force_ids"] = ["rates-repricing", "ghost-force"]
    with pytest.raises(ValidationError, match="declared forces"):
        _validated(payload)


def test_model_rejects_a_force_citing_an_unknown_input_id() -> None:
    payload = _payload()
    payload["synthesis"]["dominant_forces"][0]["source_ids"] = ["us-10y", "no-such-input"]
    with pytest.raises(ValidationError, match="unknown input IDs"):
        _validated(payload)


def test_model_rejects_a_force_citing_a_failed_input() -> None:
    payload = _payload()
    payload["inputs"]["articles"].append(
        {
            "input_id": "article-broken",
            "source": "Example",
            "title": "Broken fetch",
            "url": "https://example.com/broken",
            "published_at": "2026-07-18T00:00:00+00:00",
            "accessed_at": "2026-07-19T12:00:00+09:00",
            "status": "failed",
            "used_for": "取得失敗の記録",
        }
    )
    payload["synthesis"]["dominant_forces"][0]["source_ids"] = ["us-10y", "article-broken"]
    with pytest.raises(ValidationError, match="cannot cite failed inputs"):
        _validated(payload)


def test_model_rejects_a_topography_citing_an_unknown_input_id() -> None:
    payload = _payload()
    payload["connection"]["bargain_topography"]["source_ids"] = ["no-such-input"]
    with pytest.raises(ValidationError, match="unknown input IDs"):
        _validated(payload)


def test_model_rejects_a_partial_probability_set() -> None:
    payload = _payload()
    _set_probabilities(payload, (0.5, 0.3, None))
    with pytest.raises(ValidationError, match="all scenarios or none"):
        _validated(payload)


def test_model_rejects_an_off_grid_probability() -> None:
    payload = _payload()
    _set_probabilities(payload, (0.33, 0.33, 0.34))
    with pytest.raises(ValidationError, match=re.escape("0.05 grid")):
        _validated(payload)


def test_model_rejects_a_probability_above_the_range() -> None:
    payload = _payload()
    _set_probabilities(payload, (0.95, 0.05, 0.0))
    with pytest.raises(ValidationError, match=re.escape("between 0.05 and 0.90")):
        _validated(payload)


def test_model_rejects_a_zero_probability() -> None:
    payload = _payload()
    _set_probabilities(payload, (0.9, 0.1, 0.0))
    with pytest.raises(ValidationError, match=re.escape("between 0.05 and 0.90")):
        _validated(payload)


def test_model_rejects_probabilities_that_do_not_sum_to_one() -> None:
    payload = _payload()
    _set_probabilities(payload, (0.5, 0.3, 0.3))
    with pytest.raises(ValidationError, match=re.escape("sum to 1.0")):
        _validated(payload)


def test_model_accepts_probabilities_with_binary_representation_error() -> None:
    # 0.35 + 0.35 + 0.30 does not equal 1.0 in floats; the integer-step check must
    # accept it anyway, on every future load.
    payload = _payload()
    _set_probabilities(payload, (0.35, 0.35, 0.30))
    document = _validated(payload)
    assert sum(s.probability or 0.0 for s in document.scenarios) == pytest.approx(1.0)


# --- publication gate: presence is required for new reports only ---


def test_gate_requires_the_synthesis() -> None:
    payload = _payload(strategy_layer=False)
    with pytest.raises(ValueError, match="synthesis of dominant forces"):
        require_integrated_strategy(_validated(payload))


def test_gate_requires_scenario_probabilities() -> None:
    payload = _payload()
    _set_probabilities(payload, (None, None, None))
    with pytest.raises(ValueError, match="carry a probability"):
        require_integrated_strategy(_validated(payload))


def test_gate_requires_an_estimate_caveat() -> None:
    payload = _payload()
    payload["connection"]["estimate_caveats"] = []
    with pytest.raises(ValueError, match="estimate caveat"):
        require_integrated_strategy(_validated(payload))


def test_gate_requires_the_bargain_topography() -> None:
    payload = _payload()
    del payload["connection"]["bargain_topography"]
    with pytest.raises(ValueError, match="bargain topography"):
        require_integrated_strategy(_validated(payload))


def test_gate_rejects_a_topography_without_a_machine_snapshot_citation() -> None:
    payload = _payload()
    payload["connection"]["bargain_topography"]["source_ids"] = ["us-10y"]
    with pytest.raises(ValueError, match="market-snapshot machine input"):
        require_integrated_strategy(_validated(payload))


def test_gate_rejects_a_topography_grounded_only_in_a_non_market_snapshot_command() -> None:
    payload = _payload()
    payload["inputs"]["machine_snapshots"][0]["command"] = "baibai-engine screening run"
    with pytest.raises(ValueError, match="market-snapshot machine input"):
        require_integrated_strategy(_validated(payload))


def test_gate_rejects_a_topography_grounded_only_in_the_scorecard_snapshot(
    tmp_path: Path,
) -> None:
    # Every revision after the first is forced to carry its predecessor's scorecard
    # snapshot, so that mandatory citation must not satisfy the grounding requirement.
    payload = _payload()
    context_db = str((tmp_path / "app.sqlite").resolve())
    indicators_db = str((tmp_path / "macro.sqlite").resolve())
    digest = "0" * 64
    input_id = scorecard_snapshot_input_id(
        context_id="macro-context-2026-07-01-previous",
        snapshot_asof=date(2026, 7, 19),
        rules_revision="2026-07-25T000000+0900",
        context_db=context_db,
        indicators_db=indicators_db,
        result_digest=digest,
    )
    payload["inputs"]["machine_snapshots"] = [
        {
            "kind": "macro-scorecard-evaluation",
            "input_id": input_id,
            "context_id": "macro-context-2026-07-01-previous",
            "rules_revision": "2026-07-25T000000+0900",
            "context_db": context_db,
            "indicators_db": indicators_db,
            "result_digest": digest,
            "command": "baibai-engine macro context scorecard --format json",
            "snapshot_asof": "2026-07-19",
            "observation_as_of": None,
            "accessed_at": "2026-07-19T12:00:00+09:00",
            "status": "ok",
            "used_for": "前回シナリオの採点",
        }
    ]
    # The connection fact that cited the market snapshot has to move with it.
    payload["connection"]["fact_summary"][1]["source_ids"] = [input_id]
    payload["connection"]["bargain_topography"]["source_ids"] = [input_id]
    with pytest.raises(ValueError, match="market-snapshot machine input"):
        require_integrated_strategy(_validated(payload))


def test_publish_wires_the_integrated_strategy_gate(tmp_path: Path) -> None:
    service = MacroContextService(tmp_path / "app.sqlite")
    document = _validated(_payload(strategy_layer=False))
    with pytest.raises(ValueError, match="synthesis of dominant forces"):
        service.publish(document, expected_head=None)
    assert service.head_id() is None


def test_publish_accepts_a_complete_strategy_layer(tmp_path: Path) -> None:
    service = MacroContextService(tmp_path / "app.sqlite")
    document = _validated(_payload())
    service.publish(document, expected_head=None)
    assert service.head_id() == document.context_id
    stored = service.get(document.context_id)
    assert stored.synthesis is not None
    assert [s.probability for s in stored.scenarios] == [0.5, 0.3, 0.2]
    assert stored.connection.bargain_topography is not None
    assert stored.connection.estimate_caveats[0].affected_component == "fv_anchor"


# --- regression: revisions published before the layer keep loading everywhere ---


def test_a_revision_without_the_strategy_layer_stays_readable(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    initialize_database(path)
    legacy = _payload(strategy_layer=False, machine_conditions=[])
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO macro_context (
                context_id, schema_version, as_of, published_at, supersedes_id, payload
            ) VALUES (?, 4, ?, ?, NULL, ?)
            """,
            (
                legacy["context_id"],
                legacy["as_of"],
                legacy["published_at"],
                json.dumps(legacy),
            ),
        )
        connection.execute(
            "INSERT INTO macro_context_head(singleton, context_id) VALUES (1, ?)",
            (legacy["context_id"],),
        )

    service = MacroContextService(path)
    assert service.head_id() == legacy["context_id"]
    document = service.get(legacy["context_id"])
    assert document.synthesis is None
    assert all(s.probability is None for s in document.scenarios)
    assert document.connection.bargain_topography is None
    assert document.connection.estimate_caveats == ()
    payload = latest_macro_context_payload(path, as_of=date(2026, 7, 19))
    assert payload is not None
    assert payload["context_id"] == legacy["context_id"]
