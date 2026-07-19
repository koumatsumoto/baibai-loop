from __future__ import annotations

import hashlib
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml
from tools.research_price_watch import (
    ResearchPriceWatchError,
    _PacketCandidate,
    _read_market_observations,
    _select_latest_packets,
    build_parser,
    main,
)

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.market.sqlite import open_connection
from baibai_engine.research.decision_packet import (
    decision_packet_core_hash,
    independent_review_hash,
    load_decision_packet,
    load_independent_review,
)

ROOT = Path(__file__).parents[1]
PACKET = ROOT / "tests/fixtures/decision-packet/2331-decision.yaml"
REVIEW = ROOT / "tests/fixtures/decision-packet/2331-decision-review.yaml"
LEDGER = ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"
ASOF = date(2026, 7, 3)


def _packet_root(
    tmp_path: Path,
    *,
    recommendation: Literal["buy", "defer", "reject"] = "buy",
    packet_asof: date = ASOF,
    independent_review_ref: bool = True,
) -> Path:
    root = tmp_path / "packets"
    root.mkdir()
    canonical_dir = root / f"{packet_asof:%Y}" / f"{packet_asof:%m}"
    canonical_dir.mkdir(parents=True)
    packet_name = f"{packet_asof.isoformat()}-2331-decision.yaml"
    review_name = f"{packet_asof.isoformat()}-2331-decision-review.yaml"

    document = load_decision_packet(PACKET)
    review = load_independent_review(REVIEW)
    snapshot = document.input_snapshot.model_copy(update={"as_of": packet_asof})
    judgment = document.judgment.model_copy(update={"recommendation": recommendation})
    document = document.model_copy(
        update={
            "input_snapshot": snapshot,
            "judgment": judgment,
            "independent_review_ref": review_name if independent_review_ref else None,
        }
    )
    packet_core_hash = decision_packet_core_hash(document)
    review = review.model_copy(update={"reviewed_packet_sha256": packet_core_hash})
    override = document.human_evidence_override
    if override is not None:
        override = override.model_copy(
            update={
                "proposal_sha256": packet_core_hash,
                "review_sha256": independent_review_hash(review),
            }
        )
        document = document.model_copy(update={"human_evidence_override": override})

    (canonical_dir / packet_name).write_text(
        yaml.safe_dump(document.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    (canonical_dir / review_name).write_text(
        yaml.safe_dump(review.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return root


def _canonical_packet_path(root: Path, *, packet_asof: date = ASOF) -> Path:
    return (
        root
        / f"{packet_asof:%Y}"
        / f"{packet_asof:%m}"
        / f"{packet_asof.isoformat()}-2331-decision.yaml"
    )


def _market_sqlite(
    path: Path,
    *,
    tickers: tuple[str, ...] = ("2331",),
    sessions: tuple[date, ...] = (ASOF,),
    close_by_key: dict[tuple[str, date], float | None] | None = None,
    factor_by_key: dict[tuple[str, date], float | None] | None = None,
    omitted: set[tuple[str, date]] | None = None,
    future_bars: tuple[tuple[str, date, float, float | None], ...] = (),
) -> None:
    closes = close_by_key or {}
    factors = factor_by_key or {}
    missing = omitted or set()
    conn = open_connection(path)
    try:
        conn.executemany(
            "INSERT INTO jquants_market_calendar(day, is_business_day) VALUES (?, 1)",
            [(session.isoformat(),) for session in sessions],
        )
        conn.execute(
            "INSERT INTO source_coverage(source, coverage_key, coverage_start, coverage_end, "
            "fetched_at_utc, record_count, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "jquants_market_calendar",
                "research-watch-test",
                sessions[0].isoformat(),
                sessions[-1].isoformat(),
                "2026-07-20T00:00:00+00:00",
                len(sessions),
                "ok",
            ),
        )
        rows = [
            (
                ticker,
                session.isoformat(),
                closes.get((ticker, session), 1000.0),
                factors.get((ticker, session), 1.0),
            )
            for ticker in tickers
            for session in sessions
            if (ticker, session) not in missing
        ]
        rows.extend(
            (ticker, session.isoformat(), close, factor)
            for ticker, session, close, factor in future_bars
        )
        conn.executemany(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close, adjustment_factor) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _opening_only_ledger(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.yaml"
    path.write_text(
        """schema_version: 2
portfolio_scope: repository_only
as_of: "2026-07-03T12:00:00+09:00"
estimated_exit_tax_rate_bps: null
estimated_exit_tax_basis: null
events:
  - event_id: opening
    type: opening_balance
    occurred_at: "2026-07-01T08:00:00+09:00"
    amount_yen: 300000
market_prices: []
overrides: []
""",
        encoding="utf-8",
    )
    return path


def _run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    packets_root: Path | None = None,
    ledger: Path = LEDGER,
    sqlite_path: Path | None = None,
    asof: date = ASOF,
) -> tuple[int, dict[str, Any], str]:
    market = sqlite_path or tmp_path / "market.sqlite"
    exit_code = main(
        [
            "--packets-root",
            str(packets_root or _packet_root(tmp_path)),
            "--ledger",
            str(ledger),
            "--sqlite-path",
            str(market),
            "--asof",
            asof.isoformat(),
        ]
    )
    captured = capsys.readouterr()
    payload: dict[str, Any] = yaml.safe_load(captured.out) if captured.out else {}
    return exit_code, payload, captured.err


def test_cli_contract_has_only_explicit_inputs() -> None:
    help_text = build_parser().format_help()

    assert all(
        option in help_text for option in ("--packets-root", "--ledger", "--sqlite-path", "--asof")
    )
    assert "--write" not in help_text


def test_resolved_join_keeps_history_and_excludes_ledger_only_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    market = tmp_path / "market.sqlite"
    _market_sqlite(market, close_by_key={("2331", ASOF): 1000.0})

    exit_code, payload, stderr = _run(tmp_path, capsys, sqlite_path=market)

    assert exit_code == 0
    assert stderr == ""
    rows = payload["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert payload["market_asof"] == "2026-07-03"
    assert payload["ledger_as_of"] == "2026-07-11T10:00:00+09:00"
    assert payload["diagnostics"]["portfolio_status_basis"] == "canonical_ledger_as_of"
    assert row == {
        "ticker": "2331",
        "status": "resolved",
        "current_close_yen": 1000,
        "close_as_of": "2026-07-03",
        "packet_fair_value_yen": 1300,
        "packet_fv_gap_pct": 30.0,
        "packet_entry_price_basis_yen": 1032,
        "packet_recommendation_at_as_of": "buy",
        "packet_as_of": "2026-07-03",
        "valuation_model_version": "fv-v1",
        "re_research_required": True,
        "current_decision_status": "not_evaluated",
        "current_portfolio_status": "held",
        "reservation_history": [
            {
                "reservation_id": "reservation-2331-first",
                "occurred_at": "2026-06-02T09:00:00+09:00",
                "quantity": 200,
                "price_guard_yen": 1050,
                "expires_at": "2026-06-05T15:30:00+09:00",
            },
            {
                "reservation_id": "reservation-2331-add",
                "occurred_at": "2026-07-01T09:00:00+09:00",
                "quantity": 100,
                "price_guard_yen": 1000,
                "expires_at": "2026-07-05T15:30:00+09:00",
            },
        ],
        "unresolved_reason": None,
    }
    assert payload["coverage"]["ledger_only_tickers"] == ["8929"]
    assert {item["ticker"] for item in rows} == {"2331"}


def test_packet_only_ticker_is_included_as_unheld(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    market = tmp_path / "market.sqlite"
    _market_sqlite(market)

    exit_code, payload, _ = _run(
        tmp_path,
        capsys,
        sqlite_path=market,
        ledger=_opening_only_ledger(tmp_path),
    )

    assert exit_code == 0
    assert payload["rows"][0]["current_portfolio_status"] == "unheld"
    assert payload["rows"][0]["reservation_history"] == []


def test_held_and_reserved_comes_only_from_replayed_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = safe_load(LEDGER.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    events = raw["events"]
    assert isinstance(events, list)
    events.append(
        {
            "event_id": "reserve-2331-current",
            "type": "reservation",
            "occurred_at": "2026-07-10T09:00:00+09:00",
            "reservation_id": "reservation-2331-current",
            "order_id": "order-2331-current",
            "ticker": "2331",
            "sector": "サービス業",
            "common_factors": ["labor-automation"],
            "quantity": 100,
            "price_guard_yen": 980,
            "expires_at": "2026-07-31T15:30:00+09:00",
        }
    )
    ledger = tmp_path / "ledger.yaml"
    ledger.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    market = tmp_path / "market.sqlite"
    _market_sqlite(market)

    exit_code, payload, _ = _run(tmp_path, capsys, ledger=ledger, sqlite_path=market)

    assert exit_code == 0
    row = payload["rows"][0]
    assert row["current_portfolio_status"] == "held_and_reserved"
    assert [item["reservation_id"] for item in row["reservation_history"]] == [
        "reservation-2331-first",
        "reservation-2331-add",
        "reservation-2331-current",
    ]


def test_latest_packet_selection_uses_asof_not_path_or_mtime() -> None:
    document = load_decision_packet(PACKET)
    older = document.model_copy(
        update={
            "input_snapshot": document.input_snapshot.model_copy(update={"as_of": date(2026, 7, 2)})
        }
    )
    newer = document.model_copy(
        update={"judgment": document.judgment.model_copy(update={"recommendation": "defer"})}
    )

    selected = _select_latest_packets(
        [
            _PacketCandidate(Path("z-newer-mtime.yaml"), older),
            _PacketCandidate(Path("a-older-mtime.yaml"), newer),
        ]
    )

    assert selected["2331"].document.judgment.recommendation == "defer"


def test_same_ticker_same_asof_is_a_hard_conflict() -> None:
    document = load_decision_packet(PACKET)

    with pytest.raises(ResearchPriceWatchError, match="conflicting latest decision packets"):
        _select_latest_packets(
            [
                _PacketCandidate(Path("first.yaml"), document),
                _PacketCandidate(Path("second.yaml"), document),
            ]
        )


def test_latest_reject_does_not_fall_back_to_an_older_buy() -> None:
    document = load_decision_packet(PACKET)
    older_buy = document.model_copy(
        update={
            "input_snapshot": document.input_snapshot.model_copy(update={"as_of": date(2026, 7, 2)})
        }
    )
    latest_reject = document.model_copy(
        update={"judgment": document.judgment.model_copy(update={"recommendation": "reject"})}
    )

    selected = _select_latest_packets(
        [
            _PacketCandidate(Path("older-buy.yaml"), older_buy),
            _PacketCandidate(Path("latest-reject.yaml"), latest_reject),
        ]
    )

    assert selected["2331"].document.judgment.recommendation == "reject"


def test_latest_reject_is_excluded_from_watch_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packet_root = _packet_root(tmp_path, recommendation="reject")
    sqlite_path = tmp_path / "market.sqlite"
    _market_sqlite(sqlite_path)

    exit_code, payload, stderr = _run(
        tmp_path,
        capsys,
        packets_root=packet_root,
        sqlite_path=sqlite_path,
    )

    assert exit_code == 0
    assert stderr == ""
    assert payload["rows"] == []
    assert payload["coverage"]["excluded_latest_reject_tickers"] == ["2331"]


@pytest.mark.parametrize(
    ("close", "factor", "reason"),
    [
        (None, 1.0, "missing_raw_close:2026-07-03"),
        (0.0, 1.0, "invalid_raw_close:2026-07-03"),
        (1000.0, None, "corporate_action_basis_unassessed:2026-07-03"),
        (1000.0, 0.5, "corporate_action_in_comparison_window:2026-07-03"),
    ],
)
def test_exact_asof_invalidity_never_falls_back_to_an_older_close(
    tmp_path: Path,
    close: float | None,
    factor: float | None,
    reason: str,
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    prior = date(2026, 7, 2)
    _market_sqlite(
        sqlite_path,
        sessions=(prior, ASOF),
        close_by_key={("2331", prior): 900.0, ("2331", ASOF): close},
        factor_by_key={("2331", ASOF): factor},
    )
    document = load_decision_packet(PACKET)

    observation = _read_market_observations(
        sqlite_path,
        packets={"2331": _PacketCandidate(PACKET, document)},
        asof=ASOF,
    )["2331"]

    assert observation.unresolved_reason == reason
    expected_close = None if close in {None, 0.0} else 1000.0
    assert observation.current_close_yen == expected_close


def test_missing_exact_asof_bar_never_falls_back_to_an_older_bar(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    prior = date(2026, 7, 2)
    _market_sqlite(
        sqlite_path,
        sessions=(prior, ASOF),
        close_by_key={("2331", prior): 900.0},
        omitted={("2331", ASOF)},
    )
    document = load_decision_packet(PACKET)

    observation = _read_market_observations(
        sqlite_path,
        packets={"2331": _PacketCandidate(PACKET, document)},
        asof=ASOF,
    )["2331"]

    assert observation.current_close_yen is None
    assert observation.close_as_of is None
    assert observation.unresolved_reason == "missing_raw_bar:2026-07-03"


@pytest.mark.parametrize(
    ("factor", "reason_prefix"),
    [
        (None, "corporate_action_basis_unassessed"),
        (2.0, "corporate_action_in_comparison_window"),
    ],
)
def test_intermediate_factor_invalidates_the_whole_comparison_window(
    tmp_path: Path, factor: float | None, reason_prefix: str
) -> None:
    intermediate = date(2026, 7, 2)
    sqlite_path = tmp_path / "market.sqlite"
    _market_sqlite(
        sqlite_path,
        sessions=(intermediate, ASOF),
        factor_by_key={("2331", intermediate): factor},
    )
    document = load_decision_packet(PACKET)
    earlier_packet = document.model_copy(
        update={
            "input_snapshot": document.input_snapshot.model_copy(update={"as_of": intermediate})
        }
    )

    observation = _read_market_observations(
        sqlite_path,
        packets={"2331": _PacketCandidate(PACKET, earlier_packet)},
        asof=ASOF,
    )["2331"]

    assert observation.unresolved_reason == f"{reason_prefix}:{intermediate.isoformat()}"
    assert observation.current_close_yen == 1000.0


def test_one_ticker_can_be_unresolved_without_suppressing_other_rows(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _market_sqlite(
        sqlite_path,
        tickers=("2331", "9999"),
        omitted={("9999", ASOF)},
    )
    document = load_decision_packet(PACKET)
    packets = {
        ticker: _PacketCandidate(Path(f"{ticker}.yaml"), document) for ticker in ("2331", "9999")
    }

    observations = _read_market_observations(sqlite_path, packets=packets, asof=ASOF)

    assert observations["2331"].unresolved_reason is None
    assert observations["9999"].unresolved_reason == "missing_raw_bar:2026-07-03"


def test_future_bar_is_ignored(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _market_sqlite(
        sqlite_path,
        future_bars=(("2331", date(2026, 7, 4), 1.0, 0.5),),
    )
    document = load_decision_packet(PACKET)

    observation = _read_market_observations(
        sqlite_path,
        packets={"2331": _PacketCandidate(PACKET, document)},
        asof=ASOF,
    )["2331"]

    assert observation.unresolved_reason is None
    assert observation.current_close_yen == 1000.0


def test_calendar_internal_day_gap_fails_closed_despite_source_coverage(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    start = date(2026, 7, 1)
    _market_sqlite(sqlite_path, sessions=(start, ASOF))
    document = load_decision_packet(PACKET)
    earlier_packet = document.model_copy(
        update={"input_snapshot": document.input_snapshot.model_copy(update={"as_of": start})}
    )

    with pytest.raises(ResearchPriceWatchError, match="missing row: 2026-07-02"):
        _read_market_observations(
            sqlite_path,
            packets={"2331": _PacketCandidate(PACKET, earlier_packet)},
            asof=ASOF,
        )


@pytest.mark.parametrize(
    ("factor", "unresolved"),
    [
        (1.0 + 5e-13, False),
        (1.0 + 2e-12, True),
    ],
)
def test_adjustment_factor_one_uses_a_narrow_float_tolerance(
    tmp_path: Path, factor: float, unresolved: bool
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _market_sqlite(sqlite_path, factor_by_key={("2331", ASOF): factor})
    document = load_decision_packet(PACKET)

    observation = _read_market_observations(
        sqlite_path,
        packets={"2331": _PacketCandidate(PACKET, document)},
        asof=ASOF,
    )["2331"]

    assert (observation.unresolved_reason is not None) is unresolved


def test_future_packet_is_rejected_before_market_evaluation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    future_asof = date(2026, 7, 4)
    packet_root = _packet_root(tmp_path, packet_asof=future_asof)
    sqlite_path = tmp_path / "market.sqlite"
    _market_sqlite(sqlite_path)

    exit_code, payload, stderr = _run(
        tmp_path,
        capsys,
        packets_root=packet_root,
        sqlite_path=sqlite_path,
    )

    assert exit_code == 2
    assert payload == {}
    assert "future decision packet is not allowed" in stderr
    assert "Traceback" not in stderr


def test_override_expiry_does_not_remove_a_promoted_packet_from_the_watch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_asof = date(2026, 8, 3)
    sessions = tuple(
        ASOF + timedelta(days=offset) for offset in range((watch_asof - ASOF).days + 1)
    )
    packet_root = _packet_root(tmp_path)
    sqlite_path = tmp_path / "market.sqlite"
    _market_sqlite(sqlite_path, sessions=sessions)

    exit_code, payload, stderr = _run(
        tmp_path,
        capsys,
        packets_root=packet_root,
        sqlite_path=sqlite_path,
        asof=watch_asof,
    )

    assert exit_code == 0
    assert stderr == ""
    assert payload["market_asof"] == "2026-08-03"
    assert payload["rows"][0]["ticker"] == "2331"
    assert payload["rows"][0]["re_research_required"] is True


def test_tiny_positive_close_is_an_unresolved_row_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _market_sqlite(sqlite_path, close_by_key={("2331", ASOF): 5e-324})

    exit_code, payload, stderr = _run(tmp_path, capsys, sqlite_path=sqlite_path)

    assert exit_code == 0
    assert stderr == ""
    row = payload["rows"][0]
    assert row["status"] == "unresolved"
    assert row["packet_fv_gap_pct"] is None
    assert row["unresolved_reason"] == "fv_gap_calculation_unresolved"


def test_misnamed_packet_is_rejected_as_not_canonical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packet_root = _packet_root(tmp_path)
    packet_path = _canonical_packet_path(packet_root)
    packet_path.rename(packet_path.with_name("2331-decision.yaml"))
    sqlite_path = tmp_path / "market.sqlite"
    _market_sqlite(sqlite_path)

    exit_code, payload, stderr = _run(
        tmp_path,
        capsys,
        packets_root=packet_root,
        sqlite_path=sqlite_path,
    )

    assert exit_code == 2
    assert payload == {}
    assert "not in canonical YYYY/MM layout" in stderr
    assert "Traceback" not in stderr


def test_reject_packet_without_canonical_review_ref_is_not_promoted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packet_root = _packet_root(
        tmp_path,
        recommendation="reject",
        independent_review_ref=False,
    )
    sqlite_path = tmp_path / "market.sqlite"
    _market_sqlite(sqlite_path)

    exit_code, payload, stderr = _run(
        tmp_path,
        capsys,
        packets_root=packet_root,
        sqlite_path=sqlite_path,
    )

    assert exit_code == 2
    assert payload == {}
    assert "does not reference its canonical promoted review" in stderr
    assert "Traceback" not in stderr


def test_successful_run_is_byte_for_byte_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packet_root = _packet_root(tmp_path)
    ledger = tmp_path / "ledger.yaml"
    shutil.copyfile(LEDGER, ledger)
    sqlite_path = tmp_path / "market.sqlite"
    _market_sqlite(sqlite_path)
    inputs = [
        *sorted(path for path in packet_root.rglob("*") if path.is_file()),
        ledger,
        sqlite_path,
    ]
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}

    exit_code, _, _ = _run(
        tmp_path,
        capsys,
        packets_root=packet_root,
        ledger=ledger,
        sqlite_path=sqlite_path,
    )

    after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
    assert exit_code == 0
    assert after == before


def test_error_has_no_stdout_or_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    market = tmp_path / "market.sqlite"
    _market_sqlite(market)

    exit_code, payload, stderr = _run(
        tmp_path,
        capsys,
        packets_root=tmp_path / "missing-packets",
        sqlite_path=market,
    )

    assert exit_code == 2
    assert payload == {}
    assert stderr.startswith("error: packets root is not a directory")
    assert "Traceback" not in stderr
