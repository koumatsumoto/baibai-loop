from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from baibai_engine.appdb.write import initialize_database
from baibai_engine.macro.context.diagnostics import (
    MACRO_CONTEXT_STALE_DAYS,
    MacroContext,
    macro_context_diagnostics,
    macro_context_from_payload,
)
from baibai_engine.macro.context.models import CORE_SECTION_ORDER, MacroContextDocument
from baibai_engine.macro.context.service import MacroContextConflictError, MacroContextService
from baibai_engine.read_api.macro import latest_macro_context_payload
from baibai_engine.screening.selection.macro_fit import macro_context_summary
from tests.helpers.macro_context import macro_context_payload


def _document(
    *,
    context_id: str = "macro-context-2026-07-19-base",
    as_of: str = "2026-07-19",
    published_at: str = "2026-07-19T12:00:00+09:00",
) -> MacroContextDocument:
    return MacroContextDocument.model_validate(
        macro_context_payload(
            context_id=context_id,
            as_of=as_of,
            published_at=published_at,
        )
    )


def _context_of(document: MacroContextDocument) -> MacroContext:
    return macro_context_from_payload(document.payload(), source="fixture.yaml")


def test_document_holds_core_ten_plus_connection_and_flattens_diagnostics() -> None:
    document = _document()
    context = _context_of(document)

    assert [section.section_id for section in document.core] == list(CORE_SECTION_ORDER)
    assert document.connection.section_id == "japan_equity_loop"
    diagnostics = macro_context_diagnostics(context, asof_date=date(2026, 7, 19))
    assert diagnostics["material_deltas"] == [
        document.core[1].material_deltas[0].model_dump(mode="json")
    ]
    # Sizing cautions live only in the connection section now.
    assert diagnostics["sizing_cautions"] == [
        document.connection.sizing_cautions[0].model_dump(mode="json")
    ]
    assert diagnostics["research_questions"] == ["借換需要の大きい企業を先に確認する"]
    assert diagnostics["refresh_triggers"] == ["10年金利が現行レンジを外れる"]


def test_publish_is_immutable_and_requires_compare_and_swap_head(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    service = MacroContextService(path)
    first = _document()
    service.publish(first, expected_head=None)

    with pytest.raises(MacroContextConflictError):
        service.publish(
            _document(
                context_id="macro-context-2026-07-20-stale",
                as_of="2026-07-20",
                published_at="2026-07-20T12:00:00+09:00",
            ),
            expected_head=None,
        )
    with pytest.raises(MacroContextConflictError):
        service.publish(first, expected_head=first.context_id)

    second = _document(
        context_id="macro-context-2026-07-19-revision",
        published_at="2026-07-19T13:00:00+09:00",
    )
    service.publish(second, expected_head=first.context_id)

    assert service.head_id() == second.context_id
    assert service.latest_for(date(2026, 7, 18)) is None
    assert service.latest_for(date(2026, 7, 19)) == second
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT context_id, supersedes_id FROM macro_context ORDER BY published_at"
        ).fetchall()
    assert rows == [(first.context_id, None), (second.context_id, first.context_id)]


def test_latest_context_orders_by_publication_not_data_as_of(tmp_path: Path) -> None:
    """A later-published context with an earlier market as_of is the operative judgment."""

    path = tmp_path / "app.sqlite"
    service = MacroContextService(path)
    newer_as_of = _document(
        context_id="macro-context-2026-07-19-first",
        as_of="2026-07-19",
        published_at="2026-07-19T12:00:00+09:00",
    )
    service.publish(newer_as_of, expected_head=None)
    later_published = _document(
        context_id="macro-context-2026-07-17-refresh",
        as_of="2026-07-17",
        published_at="2026-07-19T18:00:00+09:00",
    )
    service.publish(later_published, expected_head=newer_as_of.context_id)

    assert service.latest_for(date(2026, 7, 19)) == later_published
    payload = latest_macro_context_payload(path, as_of=date(2026, 7, 19))
    assert payload is not None
    assert payload["context_id"] == later_published.context_id
    # Point-in-time discipline is unchanged: as_of after the query date stays ineligible.
    assert service.latest_for(date(2026, 7, 18)) == later_published


def test_explicit_future_context_is_rejected(tmp_path: Path) -> None:
    service = MacroContextService(tmp_path / "app.sqlite")
    document = _document()
    service.publish(document, expected_head=None)

    with pytest.raises(MacroContextConflictError, match="future"):
        service.get_for(document.context_id, as_of=date(2026, 7, 18))


def test_revision_written_under_an_earlier_contract_is_a_log_not_a_read(tmp_path: Path) -> None:
    """An older-schema revision stays in the table, but no current read path serves it.

    Without the version filter the newest row would be returned and then fail validation,
    which would also make the first publication under the new contract impossible.
    """

    path = tmp_path / "app.sqlite"
    initialize_database(path)
    legacy_id = "macro-context-2026-07-01-legacy"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO macro_context (
                context_id, schema_version, as_of, published_at, supersedes_id, payload
            ) VALUES (?, 3, '2026-07-01', '2026-07-01T12:00:00+09:00', NULL, ?)
            """,
            (legacy_id, json.dumps({"schema_version": 3, "context_id": legacy_id})),
        )
        connection.execute(
            "INSERT INTO macro_context_head(singleton, context_id) VALUES (1, ?)", (legacy_id,)
        )

    service = MacroContextService(path)
    assert service.head_id() is None
    assert service.latest_for(date(2026, 7, 19)) is None
    assert latest_macro_context_payload(path, as_of=date(2026, 7, 19)) is None

    document = _document()
    service.publish(document, expected_head=None)
    assert service.head_id() == document.context_id
    with sqlite3.connect(path) as connection:
        kept = connection.execute("SELECT count(*) FROM macro_context").fetchone()[0]
    assert kept == 2


def test_publish_rejects_a_reading_revision_that_does_not_exist(tmp_path: Path) -> None:
    """`rules_revision` is free text, so fabricated provenance must not reach the store."""

    payload = macro_context_payload()
    payload["inputs"]["reading_snapshots"][0]["rules_revision"] = "2099-01-01T000000+0900"
    document = MacroContextDocument.model_validate(payload)

    with pytest.raises(MacroContextConflictError, match="does not exist"):
        MacroContextService(tmp_path / "app.sqlite").publish(document, expected_head=None)


def test_published_report_flows_through_db_backed_screening_read_path(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    document = _document()
    MacroContextService(path).publish(document, expected_head=None)

    payload = latest_macro_context_payload(path, as_of=document.as_of)
    assert payload is not None
    context = macro_context_from_payload(payload, source=str(path))
    summary = macro_context_summary(context, asof_date=document.as_of)

    assert summary["context_id"] == document.context_id
    assert summary["age_days"] == 0
    assert summary["material_deltas"] == [
        document.core[1].material_deltas[0].model_dump(mode="json")
    ]
    assert summary["sizing_cautions"] == [
        document.connection.sizing_cautions[0].model_dump(mode="json")
    ]
    assert summary["research_questions"] == ["借換需要の大きい企業を先に確認する"]
    assert summary["refresh_triggers"] == ["10年金利が現行レンジを外れる"]
    assert summary["warnings"] == []


def test_selection_warns_when_the_head_report_is_older_than_the_policy_threshold() -> None:
    """Freshness is the reader's rule: the report never declares its own shelf life."""

    document = _document()
    context = _context_of(document)
    edge = date.fromordinal(document.as_of.toordinal() + MACRO_CONTEXT_STALE_DAYS)
    past_edge = date.fromordinal(edge.toordinal() + 1)

    assert macro_context_summary(context, asof_date=edge)["warnings"] == []
    stale = macro_context_summary(context, asof_date=past_edge)
    assert stale["warnings"] == ["macro_context_stale"]
    assert stale["age_days"] == MACRO_CONTEXT_STALE_DAYS + 1


def test_selection_surfaces_an_input_the_report_declares_as_failed() -> None:
    """Honest disclosure must reach the reader, or omitting the input is the easier path."""

    payload = macro_context_payload()
    payload["inputs"]["articles"].append(
        {
            "input_id": "unreachable-source",
            "source": "official",
            "title": "取得できなかった一次情報",
            "url": "https://example.com/source",
            "published_at": "2026-07-19T09:00:00+09:00",
            "accessed_at": "2026-07-19T12:00:00+09:00",
            "status": "failed",
            "used_for": "取得を試みたが到達できなかった",
        }
    )
    context = macro_context_from_payload(payload, source="fixture.yaml")

    summary = macro_context_summary(context, asof_date=date(2026, 7, 19))

    assert summary["failed_inputs"] == ["unreachable-source"]
    assert summary["warnings"] == ["macro_context_failed_inputs"]


def test_selection_surfaces_a_machine_snapshot_the_report_declares_as_failed() -> None:
    """Every input type must reach the reader, not only the ones added first."""

    payload = macro_context_payload()
    payload["inputs"]["machine_snapshots"].append(
        {
            "input_id": "snapshot-unavailable",
            "command": "baibai-engine screening market-snapshot",
            "snapshot_asof": "2026-07-19",
            "observation_as_of": "2026-07-17",
            "accessed_at": "2026-07-19T12:00:00+09:00",
            "status": "failed",
            "used_for": "市場内部を確認しようとしたが cache が無かった",
        }
    )
    context = macro_context_from_payload(payload, source="fixture.yaml")

    summary = macro_context_summary(context, asof_date=date(2026, 7, 19))

    assert summary["failed_inputs"] == ["snapshot-unavailable"]
    assert summary["warnings"] == ["macro_context_failed_inputs"]


def test_missing_context_summary_keeps_the_same_keys_as_a_present_one() -> None:
    asof = date(2026, 7, 19)
    present = set(macro_context_summary(_context_of(_document()), asof_date=asof))

    assert set(macro_context_summary(None, asof_date=asof)) == present


@pytest.mark.parametrize(
    "change",
    [
        {"published_at": "2026-07-18T12:00:00+09:00"},
        {"context_id": "invalid"},
        {"schema_version": 3},
    ],
)
def test_document_rejects_invalid_canonical_fields(change: dict[str, object]) -> None:
    payload = _document().payload()
    payload.update(change)
    with pytest.raises(ValidationError):
        MacroContextDocument.model_validate(payload)


def _core(payload: dict[str, Any], section_id: str) -> dict[str, Any]:
    return next(item for item in payload["core"] if item["section_id"] == section_id)


def _risk(payload: dict[str, Any]) -> dict[str, Any]:
    return _core(payload, "risk_environment")


_BYPASSES: tuple[tuple[str, Callable[[dict[str, Any]], object]], ...] = (
    (
        "naive input timestamp",
        lambda payload: payload["inputs"]["indicator_series"][0].__setitem__(
            "published_at", "2026-07-19T12:00:00"
        ),
    ),
    (
        "no inputs at all",
        lambda payload: payload.__setitem__(
            "inputs", {"articles": [], "indicator_series": [], "reading_snapshots": []}
        ),
    ),
    ("core sections out of order", lambda payload: payload["core"].reverse()),
    (
        "blank change since previous",
        lambda payload: _core(payload, "regime_summary").__setitem__("change_since_previous", " "),
    ),
    (
        "missing previous scorecard review",
        lambda payload: _core(payload, "regime_summary").__setitem__(
            "previous_scorecard_review", None
        ),
    ),
    (
        "change since previous outside the summary",
        lambda payload: _core(payload, "rates_policy").__setitem__(
            "change_since_previous", "このsectionには置かない"
        ),
    ),
    (
        "unregistered series",
        lambda payload: _core(payload, "regime_summary").__setitem__(
            "series_ids", ["not.registered"]
        ),
    ),
    (
        "provider series id instead of the canonical id",
        lambda payload: _core(payload, "regime_summary").__setitem__("series_ids", ["DGS10"]),
    ),
    (
        "series with no indicator input",
        lambda payload: _core(payload, "regime_summary").__setitem__(
            "series_ids", ["jp.policy_rate"]
        ),
    ),
    (
        "series cited without its input",
        lambda payload: _core(payload, "regime_summary").__setitem__(
            "series_ids", ["us.10y", "us.breakeven_10y"]
        ),
    ),
    (
        "unknown source id",
        lambda payload: _core(payload, "regime_summary")["judgment"].__setitem__(
            "source_ids", ["missing-input"]
        ),
    ),
    (
        "duplicate source ids",
        lambda payload: _core(payload, "regime_summary")["judgment"].__setitem__(
            "source_ids", ["us-10y", "us-10y"]
        ),
    ),
    (
        "blank monitoring condition",
        lambda payload: _core(payload, "monitoring")["monitoring_points"][0].__setitem__(
            "condition", " "
        ),
    ),
    (
        "monitoring section with no points",
        lambda payload: _core(payload, "monitoring").__setitem__("monitoring_points", []),
    ),
    ("no scenarios", lambda payload: _risk(payload).__setitem__("scenarios", [])),
    (
        "risk appetite assessment missing",
        lambda payload: _risk(payload).__setitem__("risk_environment", None),
    ),
    (
        "risk appetite assessment without falsifiers",
        lambda payload: _risk(payload)["risk_environment"].__setitem__("falsifiers", []),
    ),
    ("scenario cases out of order", lambda payload: _risk(payload)["scenarios"].reverse()),
    (
        "material delta in the regime summary",
        lambda payload: _core(payload, "regime_summary").__setitem__(
            "material_deltas", _core(payload, "rates_policy")["material_deltas"]
        ),
    ),
    (
        "no material delta anywhere",
        lambda payload: _core(payload, "rates_policy").__setitem__("material_deltas", []),
    ),
    # The loop-specific vocabulary cannot appear in a core section.
    (
        "sector tilt in a core section",
        lambda payload: _core(payload, "rates_policy").__setitem__(
            "sector_tilts", payload["connection"]["sector_tilts"]
        ),
    ),
    (
        "research priority hint in a core section",
        lambda payload: _core(payload, "rates_policy").__setitem__(
            "research_priority_hints", payload["connection"]["research_priority_hints"]
        ),
    ),
    (
        "sizing caution in a core section",
        lambda payload: _core(payload, "rates_policy").__setitem__(
            "sizing_cautions", payload["connection"]["sizing_cautions"]
        ),
    ),
    # Connection may only cite what the core cites.
    (
        "connection cites a series outside the core",
        lambda payload: payload["connection"].__setitem__("series_ids", ["us.breakeven_10y"]),
    ),
    (
        "connection with no core reference",
        lambda payload: payload["connection"].__setitem__("core_section_ids", []),
    ),
    (
        "hint without the target it applies to",
        lambda payload: payload["connection"]["research_priority_hints"][0].__setitem__(
            "applies_to", " "
        ),
    ),
    (
        "connection without a hint",
        lambda payload: payload["connection"].__setitem__("research_priority_hints", []),
    ),
    # The reading snapshot is a required input, cited by the regime summary.
    (
        "no reading snapshot input",
        lambda payload: payload["inputs"].__setitem__("reading_snapshots", []),
    ),
    (
        "reading snapshot failed",
        lambda payload: payload["inputs"]["reading_snapshots"][0].__setitem__("status", "failed"),
    ),
    (
        "reading snapshot read past the report as_of",
        lambda payload: payload["inputs"]["reading_snapshots"][0].__setitem__(
            "reading_asof", "2026-07-20"
        ),
    ),
    (
        "regime summary does not cite the reading",
        lambda payload: [
            item.__setitem__("source_ids", ["us-10y"])
            for item in (
                *_core(payload, "regime_summary")["fact_summary"],
                _core(payload, "regime_summary")["judgment"],
                _core(payload, "regime_summary")["economic_connection"],
            )
        ],
    ),
    # Every scenario carries two machine-checkable conditions inside the window.
    (
        "scenario with a single scorecard condition",
        lambda payload: _risk(payload)["scenarios"][0].__setitem__(
            "scorecard", _risk(payload)["scenarios"][0]["scorecard"][:1]
        ),
    ),
    (
        "scorecard on a series the section does not cite",
        lambda payload: _risk(payload)["scenarios"][0]["scorecard"][0].__setitem__(
            "series_id", "us.breakeven_10y"
        ),
    ),
    (
        "scorecard deadline already past as_of",
        lambda payload: _risk(payload)["scenarios"][0]["scorecard"][0].__setitem__(
            "deadline", "2026-07-19"
        ),
    ),
    (
        "scorecard deadline beyond the settleable horizon",
        lambda payload: _risk(payload)["scenarios"][0]["scorecard"][0].__setitem__(
            "deadline", "2029-01-31"
        ),
    ),
    (
        "scorecard deadline too near for the series to print again",
        lambda payload: _risk(payload)["scenarios"][0]["scorecard"][0].__setitem__(
            "deadline", "2026-07-25"
        ),
    ),
    (
        "the same scorecard condition written twice",
        lambda payload: _risk(payload)["scenarios"][0].__setitem__(
            "scorecard", [_risk(payload)["scenarios"][0]["scorecard"][0]] * 2
        ),
    ),
    (
        "non-finite scorecard threshold",
        lambda payload: _risk(payload)["scenarios"][0]["scorecard"][0].__setitem__(
            "threshold", float("inf")
        ),
    ),
    (
        "reading snapshot older than the report's own as_of window",
        lambda payload: payload["inputs"]["reading_snapshots"][0].__setitem__(
            "reading_asof", "2026-06-01"
        ),
    ),
    (
        "context_id date disagreeing with as_of",
        lambda payload: payload.__setitem__("context_id", "macro-context-2026-07-01-base"),
    ),
    (
        "blank document summary",
        lambda payload: payload.__setitem__("summary", "   "),
    ),
    (
        "machine snapshot taken after as_of",
        lambda payload: payload["inputs"]["machine_snapshots"][0].__setitem__(
            "snapshot_asof", "2026-07-20"
        ),
    ),
    (
        "machine snapshot observing past its own as_of",
        lambda payload: payload["inputs"]["machine_snapshots"][0].__setitem__(
            "observation_as_of", "2026-07-20"
        ),
    ),
    (
        "machine snapshot id colliding with another input",
        lambda payload: payload["inputs"]["machine_snapshots"][0].__setitem__("input_id", "us-10y"),
    ),
)


@pytest.mark.parametrize(
    "mutate",
    [item[1] for item in _BYPASSES],
    ids=[item[0] for item in _BYPASSES],
)
def test_document_rejects_validator_bypass(mutate: Callable[[dict[str, Any]], object]) -> None:
    payload = _document().payload()
    mutate(payload)
    with pytest.raises(ValidationError):
        MacroContextDocument.model_validate(payload)


def test_connection_must_name_the_core_sections_its_series_come_from() -> None:
    """Otherwise `core_section_ids` is decorative and the trail back into core is lost."""

    payload = _document().payload()
    extra_input = "us-breakeven"
    payload["inputs"]["indicator_series"].append(
        {
            "input_id": extra_input,
            "provider": "fred",
            "series_id": "us.breakeven_10y",
            "window": "2026-07-01/2026-07-17",
            "observation_as_of": "2026-07-17",
            "published_at": "2026-07-17T16:00:00-04:00",
            "accessed_at": "2026-07-19T12:00:00+09:00",
            "status": "ok",
            "used_for": "期待インフレの確認",
        }
    )
    valuation = _core(payload, "valuation")
    valuation["series_ids"] = ["us.10y", "us.breakeven_10y"]
    valuation["fact_summary"][0]["source_ids"] = ["us-10y", extra_input]
    connection = payload["connection"]
    connection["series_ids"] = ["us.breakeven_10y"]
    connection["fact_summary"][0]["source_ids"] = [extra_input]
    connection["judgment"]["source_ids"] = [extra_input]
    connection["research_priority_hints"][0]["source_ids"] = [extra_input]
    connection["sector_tilts"][0]["source_ids"] = [extra_input]
    connection["sizing_cautions"][0]["source_ids"] = [extra_input]

    # Naming the section the series actually comes from validates...
    connection["core_section_ids"] = ["valuation"]
    MacroContextDocument.model_validate(payload)

    # ...naming an unrelated section does not.
    connection["core_section_ids"] = ["rates_policy"]
    with pytest.raises(ValidationError, match="core_section_ids"):
        MacroContextDocument.model_validate(payload)


@pytest.mark.parametrize("successful_source_kind", ["article", "other_indicator"])
def test_document_rejects_failed_series_hidden_by_successful_source(
    successful_source_kind: str,
) -> None:
    payload = _document().payload()
    payload["inputs"]["indicator_series"][0]["status"] = "failed"
    if successful_source_kind == "article":
        payload["inputs"]["articles"].append(
            {
                "input_id": "successful-other-source",
                "source": "official",
                "title": "補助資料",
                "url": "https://example.com/source",
                "published_at": "2026-07-19T09:00:00+09:00",
                "accessed_at": "2026-07-19T12:00:00+09:00",
                "status": "ok",
                "used_for": "補助確認",
            }
        )
    else:
        payload["inputs"]["indicator_series"].append(
            {
                "input_id": "successful-other-source",
                "provider": "fred",
                "series_id": "us.fed_funds.upper",
                "window": "2026-07-01/2026-07-17",
                "observation_as_of": "2026-07-17",
                "published_at": "2026-07-17T16:00:00-04:00",
                "accessed_at": "2026-07-19T12:00:00+09:00",
                "status": "ok",
                "used_for": "補助確認",
            }
        )
    _core(payload, "regime_summary")["fact_summary"][0]["source_ids"] = [
        "us-10y",
        "successful-other-source",
    ]

    with pytest.raises(ValidationError):
        MacroContextDocument.model_validate(payload)
