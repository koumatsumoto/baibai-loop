from __future__ import annotations

from pathlib import Path

from baibai_loop.validate.trade import (
    discover_trade_files,
    validate_trade_file,
)


def _trade_text(
    *,
    status: str = "ordered",
    ticker: str = "9682",
    order_date: str | None = "2026-05-04",
    expected_fill_at: str | None = "2026-05-07T09:00:00+09:00",
    entry_date: str | None = None,
    entry_price: float | None = None,
    paper_proxy_position_size_oku: float | None = 0.002028,
    paper_proxy_position_size_pct: float | None = 0.2028,
    real_capital_yen: float | None = 500000,
    real_order_notional_yen: float | None = 202800,
    real_concentration_pct: float | None = 40.56,
    tactical_capital_yen: float | None = None,
    tactical_concentration_pct: float | None = None,
    exit_date: str | None = None,
    exit_price: float | None = None,
    pnl_pct: float | None = None,
) -> str:
    def fmt(value: object) -> str:
        if value is None:
            return "null"
        if isinstance(value, str):
            return f'"{value}"'
        return str(value)

    return f"""---
ticker: "{ticker}"
name: "ＤＴＳ"
research_ref: records/04-research/2026/05/2026-05-04-9682-valuation-reversion.md
order_date: {fmt(order_date)}
expected_fill_at: {fmt(expected_fill_at)}
entry_date: {fmt(entry_date)}
entry_price: {fmt(entry_price)}
paper_proxy_position_size_oku: {paper_proxy_position_size_oku}
paper_proxy_position_size_pct: {paper_proxy_position_size_pct}
real_capital_yen: {fmt(real_capital_yen)}
real_order_notional_yen: {fmt(real_order_notional_yen)}
real_concentration_pct: {fmt(real_concentration_pct)}
tactical_capital_yen: {fmt(tactical_capital_yen)}
tactical_concentration_pct: {fmt(tactical_concentration_pct)}
status: {status}
exit_date: {fmt(exit_date)}
exit_price: {fmt(exit_price)}
pnl_pct: {fmt(pnl_pct)}
---

# Trade
"""


def _write_trade(tmp_path: Path, text: str, name: str = "2026-05-04-9682.md") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def test_ordered_trade_with_filled_lifecycle_passes(tmp_path: Path) -> None:
    path = _write_trade(tmp_path, _trade_text())
    assert [finding for finding in validate_trade_file(path) if finding.severity == "error"] == []


def test_ordered_trade_missing_expected_fill_at_is_flagged(tmp_path: Path) -> None:
    path = _write_trade(tmp_path, _trade_text(expected_fill_at=None))
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.lifecycle-null-where-required" in codes


def test_ordered_trade_with_entry_date_set_is_flagged(tmp_path: Path) -> None:
    path = _write_trade(tmp_path, _trade_text(entry_date="2026-05-07"))
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.lifecycle-non-null-where-prohibited" in codes


def test_open_trade_requires_entry_date(tmp_path: Path) -> None:
    path = _write_trade(
        tmp_path,
        _trade_text(status="open", entry_date=None, entry_price=1014.0),
    )
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.lifecycle-null-where-required" in codes


def test_paper_proxy_pct_inconsistent_with_oku_is_flagged(tmp_path: Path) -> None:
    path = _write_trade(
        tmp_path,
        _trade_text(paper_proxy_position_size_oku=0.002, paper_proxy_position_size_pct=0.5),
    )
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.paper-proxy-pct-mismatch" in codes


def test_real_concentration_inconsistent_with_notional_is_flagged(tmp_path: Path) -> None:
    path = _write_trade(
        tmp_path,
        _trade_text(
            real_order_notional_yen=100000, real_capital_yen=500000, real_concentration_pct=99.0
        ),
    )
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.real-concentration-mismatch" in codes


def test_real_concentration_above_hard_cap_is_error(tmp_path: Path) -> None:
    path = _write_trade(
        tmp_path,
        _trade_text(
            real_order_notional_yen=300000, real_capital_yen=500000, real_concentration_pct=60.0
        ),
    )
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.real-concentration-hard-cap" in codes


def test_real_concentration_above_soft_cap_is_warning(tmp_path: Path) -> None:
    path = _write_trade(
        tmp_path,
        _trade_text(
            real_order_notional_yen=200000, real_capital_yen=500000, real_concentration_pct=40.0
        ),
    )
    findings = validate_trade_file(path)
    soft_cap_warnings = [f for f in findings if f.code == "trade.real-concentration-soft-cap"]
    assert soft_cap_warnings
    assert soft_cap_warnings[0].severity == "warning"


def test_tactical_concentration_inconsistent_with_notional_is_flagged(tmp_path: Path) -> None:
    path = _write_trade(
        tmp_path,
        _trade_text(
            real_capital_yen=5000000,
            real_order_notional_yen=202800,
            real_concentration_pct=4.06,
            tactical_capital_yen=1000000,
            tactical_concentration_pct=40.56,
        ),
    )
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.tactical-concentration-mismatch" in codes


def test_tactical_capital_must_not_exceed_real_capital(tmp_path: Path) -> None:
    path = _write_trade(
        tmp_path,
        _trade_text(
            real_capital_yen=1000000,
            real_order_notional_yen=202800,
            real_concentration_pct=20.28,
            tactical_capital_yen=5000000,
            tactical_concentration_pct=4.06,
        ),
    )
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.tactical-capital-exceeds-real-capital" in codes


def test_partial_tactical_fields_are_flagged(tmp_path: Path) -> None:
    path = _write_trade(
        tmp_path,
        _trade_text(
            real_capital_yen=5000000,
            real_order_notional_yen=202800,
            real_concentration_pct=4.06,
            tactical_capital_yen=1000000,
        ),
    )
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.tactical-concentration-missing-field" in codes


def test_filename_date_must_match_order_date(tmp_path: Path) -> None:
    path = _write_trade(tmp_path, _trade_text(order_date="2026-05-01"))
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.filename-date-mismatch" in codes


def test_filename_ticker_must_match_front_matter(tmp_path: Path) -> None:
    text = _trade_text(ticker="1111")
    path = tmp_path / "2026-05-04-9682.md"
    path.write_text(text)
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.filename-ticker-mismatch" in codes


def test_unknown_status_is_flagged(tmp_path: Path) -> None:
    path = _write_trade(tmp_path, _trade_text(status="exited"))
    codes = {finding.code for finding in validate_trade_file(path)}
    assert "trade.unknown-status" in codes


def test_discover_trade_files_skips_template(tmp_path: Path) -> None:
    (tmp_path / "template.md").write_text("placeholder")
    valid = tmp_path / "2026-05-04-9682.md"
    valid.write_text(_trade_text())
    discovered = discover_trade_files(tmp_path)
    assert discovered == [valid]
