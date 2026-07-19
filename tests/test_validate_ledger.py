from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.cli import main as position_main
from baibai_engine.position.ledger import PortfolioLedgerError, load_portfolio_ledger
from baibai_engine.validation.cli import main as validation_main
from baibai_engine.validation.ledger import discover_ledger_files, validate_ledger_file

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"


def _raw() -> dict[str, object]:
    raw = safe_load(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _write(path: Path, raw: dict[str, object]) -> Path:
    raw = copy.deepcopy(raw)
    prices = raw.get("market_prices")
    if path.name == "portfolio-ledger.yaml" and isinstance(prices, list):
        for price in prices:
            if isinstance(price, dict) and price.get("source_kind") == "test_fixture":
                price["source_kind"] = "licensed_dataset"
                price["source_ref"] = "test-licensed-dataset"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def test_representative_fixture_passes_schema_and_reconciliation() -> None:
    assert validate_ledger_file(FIXTURE) == []


def test_canonical_ledger_rejects_test_fixture_prices(tmp_path: Path) -> None:
    path = tmp_path / "portfolio-ledger.yaml"
    path.write_text(
        yaml.safe_dump(_raw(), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    findings = validate_ledger_file(path)

    assert len(findings) == 1
    assert findings[0].code == "ledger.reconciliation"
    assert "cannot use test_fixture prices" in findings[0].message

    with pytest.raises(PortfolioLedgerError, match="cannot use test_fixture prices"):
        load_portfolio_ledger(path)


def test_schema_rejects_external_assets_and_unknown_fields(tmp_path: Path) -> None:
    raw = _raw()
    raw["external_assets"] = [{"ticker": "AAPL", "value_yen": 1_000_000}]
    path = _write(tmp_path / "portfolio-ledger.yaml", raw)

    findings = validate_ledger_file(path)

    assert {finding.code for finding in findings} == {"ledger.additionalProperties"}


def test_semantic_cash_failure_is_reported_as_validation_error(tmp_path: Path) -> None:
    raw = _raw()
    events = raw["events"]
    assert isinstance(events, list)
    events[0]["amount_yen"] = 100_000
    events[1]["amount_yen"] = 1
    path = _write(tmp_path / "portfolio-ledger.yaml", raw)

    findings = validate_ledger_file(path)

    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert findings[0].code == "ledger.reconciliation"
    assert "insufficient available cash" in findings[0].message


def test_concentration_remains_a_warning_when_overridden(tmp_path: Path) -> None:
    raw = _raw()
    events = raw["events"]
    assert isinstance(events, list)
    opening = events[0]
    assert isinstance(opening, dict)
    opening["amount_yen"] = 1_000_000
    raw["overrides"] = [
        {
            "override_id": "override-2331",
            "scope": "ticker",
            "key": "2331",
            "reason": "Reviewed permanent-loss risk before accepting concentration.",
            "decision_reference": "decision-2331",
            "approved_at": "2026-07-01T09:00:00+09:00",
            "expires_at": "2026-07-20T15:30:00+09:00",
        }
    ]
    path = _write(tmp_path / "portfolio-ledger.yaml", raw)

    findings = validate_ledger_file(path)

    ticker = next(
        finding for finding in findings if finding.code == "portfolio.ticker-concentration"
    )
    assert ticker.severity == "warning"
    assert "override=override-2331" in ticker.message


def test_discovery_only_accepts_the_canonical_ledger_name(tmp_path: Path) -> None:
    root = tmp_path / "records/04-position"
    _write(root / "other.yaml", _raw())
    assert discover_ledger_files(root) == []

    canonical = _write(root / "portfolio-ledger.yaml", _raw())
    assert discover_ledger_files(root) == [canonical]


def test_validation_cli_includes_ledger_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path
    _write(root / "records/04-position/portfolio-ledger.yaml", _raw())

    assert validation_main(["--root", str(root), "--target", "ledger"]) == 0


def test_position_cli_emits_read_only_reconciled_snapshot(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert position_main(["ledger", "--root", str(ROOT), "--ledger", str(FIXTURE)]) == 0
    output = yaml.safe_load(capsys.readouterr().out)
    assert output["available_cash_yen"] == 10_080_500
    assert output["reserved_cash_yen"] == 119_000
    assert output["portfolio_scope"] == "repository_only"


def test_duplicate_event_id_fails_schema_semantics(tmp_path: Path) -> None:
    raw = _raw()
    events = raw["events"]
    assert isinstance(events, list)
    duplicate = copy.deepcopy(events[-1])
    events.append(duplicate)
    path = _write(tmp_path / "portfolio-ledger.yaml", raw)

    findings = validate_ledger_file(path)

    assert len(findings) == 1
    assert findings[0].code == "ledger.reconciliation"
    assert "event_id must be unique" in findings[0].message
