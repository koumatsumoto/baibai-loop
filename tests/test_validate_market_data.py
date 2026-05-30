from __future__ import annotations

from pathlib import Path

import yaml

from baibai_loop.validate.market_data import (
    discover_market_data_files,
    validate_market_data_file,
)


def _market_data_payload(**overrides: object) -> dict[str, object]:
    observation: dict[str, object] = {
        "decision_event_id": "decision-20260425-2767-research",
        "ticker": "2767",
        "tracking_horizon": "plus_15bd",
        "target_date": "2026-05-15",
        "resolved_trade_date": "2026-05-15",
        "price": 1500,
        "price_basis": "close_unadjusted",
        "source_name": "manual",
        "source_url": "https://example.com/2767",
        "fetched_at": "2026-05-15T18:00:00+09:00",
        "corporate_action_checked": True,
        "same_basis_group_id": "2026-05-15-close",
        "provisional": False,
    }
    observation.update(overrides)
    return {"observations": [observation]}


def _write_market_data(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "records/_market-data/2026/05/fallback.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def test_valid_market_data_passes(tmp_path: Path) -> None:
    path = _write_market_data(tmp_path, _market_data_payload())
    assert [
        finding for finding in validate_market_data_file(path) if finding.severity == "error"
    ] == []


def test_market_data_rejects_missing_decision_event_id(tmp_path: Path) -> None:
    payload = _market_data_payload()
    observation = payload["observations"][0]
    assert isinstance(observation, dict)
    del observation["decision_event_id"]
    path = _write_market_data(tmp_path, payload)
    codes = {finding.code for finding in validate_market_data_file(path)}
    assert "market-data.required" in codes


def test_market_data_requires_provisional_for_uncertain_observations(tmp_path: Path) -> None:
    cases = (
        {"corporate_action_checked": False},
        {"price_basis": "intraday_last"},
    )
    for index, overrides in enumerate(cases):
        path = _write_market_data(tmp_path / str(index), _market_data_payload(**overrides))
        codes = {finding.code for finding in validate_market_data_file(path)}
        assert {
            "market-data.corporate-action-provisional",
            "market-data.intraday-provisional",
        } & codes


def test_discover_market_data_files(tmp_path: Path) -> None:
    path = _write_market_data(tmp_path, _market_data_payload())
    assert discover_market_data_files(tmp_path / "records/_market-data") == [path]
