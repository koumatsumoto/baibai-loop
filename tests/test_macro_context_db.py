from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from baibai_engine.macro.context import macro_context_diagnostics, macro_context_from_payload
from baibai_engine.macro.models import MacroContextDocument
from baibai_engine.macro.service import MacroContextConflictError, MacroContextService
from baibai_engine.read_api.macro import latest_macro_context_payload
from baibai_engine.screening.selection.macro_fit import macro_context_summary
from tests.helpers.macro_context import macro_context_payload


def _document(
    *,
    context_id: str = "macro-context-2026-07-19-base",
    as_of: str = "2026-07-19",
    valid_until: str = "2026-08-19",
    published_at: str = "2026-07-19T12:00:00+09:00",
) -> MacroContextDocument:
    return MacroContextDocument.model_validate(
        macro_context_payload(
            context_id=context_id,
            as_of=as_of,
            valid_until=valid_until,
            published_at=published_at,
        )
    )


def test_document_holds_fixed_eight_section_report_and_flattens_diagnostics() -> None:
    document = _document()
    context = macro_context_from_payload(document.payload(), source=Path("fixture.yaml"))

    assert [section.section_id for section in document.sections] == [
        "regime_summary",
        "rates_policy",
        "growth_demand",
        "inflation_costs",
        "fx_liquidity",
        "japan_specific",
        "scenarios_connections",
        "monitoring_points",
    ]
    diagnostics = macro_context_diagnostics(context, asof_date=date(2026, 7, 19))
    assert diagnostics["material_deltas"] == [
        document.sections[1].material_deltas[0].model_dump(mode="json")
    ]
    assert diagnostics["sizing_cautions"] == [
        document.sections[6].sizing_cautions[0].model_dump(mode="json")
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


def test_explicit_future_context_is_rejected(tmp_path: Path) -> None:
    service = MacroContextService(tmp_path / "app.sqlite")
    document = _document()
    service.publish(document, expected_head=None)

    with pytest.raises(MacroContextConflictError, match="future"):
        service.get_for(document.context_id, as_of=date(2026, 7, 18))


def test_published_report_flows_through_db_backed_screening_read_path(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    document = _document()
    MacroContextService(path).publish(document, expected_head=None)

    payload = latest_macro_context_payload(path, as_of=document.as_of)
    assert payload is not None
    context = macro_context_from_payload(payload, source=path)
    summary = macro_context_summary(context, asof_date=document.as_of)

    assert summary["context_id"] == document.context_id
    assert summary["material_deltas"] == [
        document.sections[1].material_deltas[0].model_dump(mode="json")
    ]
    assert summary["sizing_cautions"] == [
        document.sections[6].sizing_cautions[0].model_dump(mode="json")
    ]
    assert summary["research_questions"] == ["借換需要の大きい企業を先に確認する"]
    assert summary["refresh_triggers"] == ["10年金利が現行レンジを外れる"]


@pytest.mark.parametrize(
    "change",
    [
        {"valid_until": "2026-07-18"},
        {"published_at": "2026-07-18T12:00:00+09:00"},
        {"context_id": "invalid"},
    ],
)
def test_document_rejects_invalid_canonical_fields(change: dict[str, str]) -> None:
    payload = _document().payload()
    payload.update(change)
    with pytest.raises(ValidationError):
        MacroContextDocument.model_validate(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["inputs"]["indicator_series"][0].__setitem__(
            "published_at", "2026-07-19T12:00:00"
        ),
        lambda payload: payload.__setitem__("inputs", {"articles": [], "indicator_series": []}),
        lambda payload: payload["sections"].reverse(),
        lambda payload: payload["sections"][0].__setitem__("change_since_previous", " "),
        lambda payload: payload["sections"][1].__setitem__(
            "change_since_previous", "このsectionには置かない"
        ),
        lambda payload: payload["sections"][0].__setitem__("series_ids", ["not.registered"]),
        lambda payload: payload["sections"][0].__setitem__("series_ids", ["DGS10"]),
        lambda payload: payload["inputs"]["indicator_series"][0].__setitem__("series_id", "DGS10"),
        lambda payload: payload["sections"][0].__setitem__("series_ids", ["jp.policy_rate"]),
        lambda payload: payload["sections"][0].__setitem__(
            "series_ids", ["us.10y", "us.breakeven_10y"]
        ),
        lambda payload: payload["sections"][0]["judgment"].__setitem__(
            "source_ids", ["missing-input"]
        ),
        lambda payload: payload["sections"][0]["judgment"].__setitem__(
            "source_ids", ["us-10y", "us-10y"]
        ),
        lambda payload: payload["sections"][7]["monitoring_points"][0].__setitem__(
            "condition", " "
        ),
        lambda payload: payload["sections"][6].__setitem__("scenarios", []),
        lambda payload: payload["sections"][1]["investment_connection"].__setitem__(
            "sector_tilts", ["配置違反"]
        ),
        lambda payload: payload["sections"][1]["investment_connection"].__setitem__(
            "research_priority_hints", ["配置違反"]
        ),
        lambda payload: payload["sections"][7].__setitem__("monitoring_points", []),
        lambda payload: [
            section.__setitem__("material_deltas", []) or section.__setitem__("sizing_cautions", [])
            for section in payload["sections"]
        ],
    ],
)
def test_document_rejects_validator_bypass(mutate: object) -> None:
    payload = _document().payload()
    assert callable(mutate)
    mutate(payload)
    with pytest.raises(ValidationError):
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
    payload["sections"][0]["fact_summary"][0]["source_ids"] = [
        "us-10y",
        "successful-other-source",
    ]

    with pytest.raises(ValidationError):
        MacroContextDocument.model_validate(payload)
