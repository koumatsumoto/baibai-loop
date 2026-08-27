"""Contract tests for the integrated strategy layer of the macro context.

Split by the layer that owns each rule: internal consistency (force references,
series containment, probability arithmetic) lives on the document model and runs on
every load, while synthesis and probability presence are a publication gate so that
revisions published before the fields keep loading.
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
from tests.helpers.macro_context import macro_context_payload

from baibai_engine.appdb.write import initialize_database
from baibai_engine.macro.context.models import (
    MacroContextDocument,
    require_integrated_strategy,
)
from baibai_engine.macro.context.service import MacroContextService
from baibai_engine.read_api.macro import latest_macro_context_payload

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


def test_model_accepts_a_single_dominant_force_without_interactions() -> None:
    payload = _payload()
    payload["synthesis"]["dominant_forces"] = payload["synthesis"]["dominant_forces"][:1]
    payload["synthesis"]["interactions"] = []
    document = _validated(payload)
    assert len(document.synthesis.dominant_forces) == 1
    assert document.synthesis.interactions == ()


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
    # cross-channel claim nominal: the force would "cross" into valuation on paper only.
    payload = _payload()
    force = payload["synthesis"]["dominant_forces"][1]
    assert force["core_section_ids"] == ["fx", "valuation"]
    force["series_ids"] = ["usd_jpy"]
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


def _append_failed_article(payload: dict[str, Any]) -> str:
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
    return "article-broken"


def test_model_rejects_a_force_citing_a_failed_input() -> None:
    payload = _payload()
    failed_id = _append_failed_article(payload)
    payload["synthesis"]["dominant_forces"][0]["source_ids"] = ["us-10y", failed_id]
    with pytest.raises(ValidationError, match="cannot cite failed inputs"):
        _validated(payload)


def test_model_rejects_an_interaction_citing_an_unknown_input_id() -> None:
    payload = _payload()
    payload["synthesis"]["interactions"][0]["source_ids"] = ["no-such-input"]
    with pytest.raises(ValidationError, match="unknown input IDs"):
        _validated(payload)


def test_model_rejects_an_interaction_citing_a_failed_input() -> None:
    payload = _payload()
    failed_id = _append_failed_article(payload)
    payload["synthesis"]["interactions"][0]["source_ids"] = ["us-10y", failed_id]
    with pytest.raises(ValidationError, match="cannot cite failed inputs"):
        _validated(payload)


def test_model_rejects_a_topography_citing_an_unknown_input_id() -> None:
    payload = _payload()
    payload["connection"]["bargain_topography"]["source_ids"] = ["no-such-input"]
    with pytest.raises(ValidationError, match="unknown input IDs"):
        _validated(payload)


def test_model_rejects_a_topography_citing_a_failed_input() -> None:
    payload = _payload()
    failed_id = _append_failed_article(payload)
    payload["connection"]["bargain_topography"]["source_ids"] = [failed_id]
    with pytest.raises(ValidationError, match="cannot cite failed inputs"):
        _validated(payload)


def test_model_rejects_an_estimate_caveat_citing_an_unknown_input_id() -> None:
    payload = _payload()
    payload["connection"]["estimate_caveats"][0]["source_ids"] = ["no-such-input"]
    with pytest.raises(ValidationError, match="unknown input IDs"):
        _validated(payload)


def test_model_rejects_an_estimate_caveat_citing_a_failed_input() -> None:
    payload = _payload()
    failed_id = _append_failed_article(payload)
    payload["connection"]["estimate_caveats"][0]["source_ids"] = ["us-10y", failed_id]
    with pytest.raises(ValidationError, match="cannot cite failed inputs"):
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


def test_model_accepts_probabilities_at_the_grid_boundaries() -> None:
    # 0.90 and 0.05 are the widest and narrowest weights the grid allows; the bounds
    # must accept them exactly, or the honest extreme becomes unwritable.
    payload = _payload()
    _set_probabilities(payload, (0.90, 0.05, 0.05))
    document = _validated(payload)
    assert [s.probability for s in document.scenarios] == [0.90, 0.05, 0.05]


def test_publication_gate_requires_the_synthesis() -> None:
    with pytest.raises(ValueError, match="synthesis of dominant forces"):
        require_integrated_strategy(_validated(_payload(strategy_layer=False)))


def test_publication_gate_requires_scenario_probabilities() -> None:
    payload = _payload()
    _set_probabilities(payload, (None, None, None))
    with pytest.raises(ValueError, match="carry a probability"):
        require_integrated_strategy(_validated(payload))


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
    legacy = _payload(strategy_layer=False)
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
