from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from baibai_engine.position import cli as position_cli
from baibai_engine.position.cli import main
from baibai_engine.position.ledger import (
    load_portfolio_ledger,
    reconcile_portfolio,
    replay_events_through,
    reservation_snapshots,
)
from baibai_engine.position.result_recording import ResultRecordingError, record_result

FIXTURE = Path(__file__).parent / "fixtures" / "portfolio-ledger" / "representative.yaml"
JST = ZoneInfo("Asia/Tokyo")
PROPOSAL = "https://github.com/koumatsumoto/baibai-loop/issues/360#issuecomment-1"


def _at(hour: int) -> datetime:
    return datetime(2026, 7, 12, hour, 0, tzinfo=JST)


def test_open_report_creates_a_source_bound_reservation() -> None:
    result = record_result(
        load_portfolio_ledger(FIXTURE),
        proposal_ref=PROPOSAL,
        status="open",
        occurred_at=_at(9),
        ticker="1234",
        quantity=100,
        sector="サービス業",
        price_guard_yen=Decimal("900"),
        expires_at=datetime(2026, 7, 20, 15, 30, tzinfo=JST),
        now=_at(12),
    )
    assert result.changed is True
    reservation = reconcile_portfolio(result.document).active_reservations[-1]
    assert reservation.ticker == "1234"
    assert result.document.events[-1].decision_reference == PROPOSAL


def test_filled_report_consumes_the_matching_active_reservation() -> None:
    opened = record_result(
        load_portfolio_ledger(FIXTURE),
        proposal_ref=PROPOSAL,
        status="open",
        occurred_at=_at(9),
        ticker="2331",
        quantity=100,
        sector="サービス業",
        price_guard_yen=Decimal("1100"),
        expires_at=datetime(2026, 7, 20, 15, 30, tzinfo=JST),
        now=_at(12),
    )
    reservation_id = next(
        item.reservation_id
        for item in reconcile_portfolio(opened.document).active_reservations
        if item.ticker == "2331"
    )
    result = record_result(
        opened.document,
        proposal_ref=PROPOSAL,
        status="filled",
        occurred_at=_at(10),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1090"),
        reservation_id=reservation_id,
        now=_at(12),
    )
    snapshot = reconcile_portfolio(result.document)
    assert all(item.reservation_id != reservation_id for item in snapshot.active_reservations)
    assert any(item.ticker == "2331" and item.quantity == 300 for item in snapshot.holdings)


def test_cancelled_report_releases_only_the_reported_reservation() -> None:
    result = record_result(
        load_portfolio_ledger(FIXTURE),
        proposal_ref=PROPOSAL,
        status="cancelled",
        occurred_at=_at(10),
        reservation_id="reservation-8929-pending",
        now=_at(12),
    )
    assert (
        reservation_snapshots(replay_events_through(result.document.events, result.document.as_of))
        == ()
    )
    repeated = record_result(
        result.document,
        proposal_ref=PROPOSAL,
        status="cancelled",
        occurred_at=_at(10),
        reservation_id="reservation-8929-pending",
        now=_at(12),
    )
    assert repeated.changed is False


def test_expired_report_requires_expiry_and_is_idempotent() -> None:
    ledger = load_portfolio_ledger(FIXTURE)
    expiry = datetime(2026, 7, 31, 15, 30, tzinfo=JST)
    now = datetime(2026, 8, 1, 12, 0, tzinfo=JST)
    with pytest.raises(ResultRecordingError, match="at or after expires_at"):
        record_result(
            ledger,
            proposal_ref=PROPOSAL,
            status="expired",
            occurred_at=datetime(2026, 7, 31, 15, 29, tzinfo=JST),
            reservation_id="reservation-8929-pending",
            now=now,
        )

    result = record_result(
        ledger,
        proposal_ref=PROPOSAL,
        status="expired",
        occurred_at=expiry,
        reservation_id="reservation-8929-pending",
        now=now,
    )
    release = result.document.events[-1]
    assert release.type == "release"
    assert release.reason == "expired"
    assert (
        reservation_snapshots(replay_events_through(result.document.events, result.document.as_of))
        == ()
    )
    repeated = record_result(
        result.document,
        proposal_ref=PROPOSAL,
        status="expired",
        occurred_at=expiry,
        reservation_id="reservation-8929-pending",
        now=now,
    )
    assert repeated.changed is False


def test_expired_report_requires_explicit_reservation_id() -> None:
    with pytest.raises(ResultRecordingError, match="expired requires reservation_id"):
        record_result(
            load_portfolio_ledger(FIXTURE),
            proposal_ref=PROPOSAL,
            status="expired",
            occurred_at=datetime(2026, 7, 31, 15, 30, tzinfo=JST),
            now=datetime(2026, 8, 1, 12, 0, tzinfo=JST),
        )


def test_cli_expired_requires_explicit_reservation_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ledger.yaml"
    ledger.write_bytes(FIXTURE.read_bytes())
    assert (
        main(
            [
                "record-result",
                "--root",
                str(tmp_path),
                "--ledger",
                "ledger.yaml",
                "--proposal-ref",
                PROPOSAL,
                "--status",
                "expired",
                "--occurred-at",
                "2026-07-31T15:30:00+09:00",
                "--out",
                "expired.yaml",
            ],
            now=datetime(2026, 8, 1, 12, 0, tzinfo=JST),
        )
        == 2
    )
    assert "expired requires reservation_id" in capsys.readouterr().err
    assert not (tmp_path / "expired.yaml").exists()


def test_expired_report_releases_only_remaining_partial_fill_quantity() -> None:
    expiry = datetime(2026, 7, 20, 15, 30, tzinfo=JST)
    now = datetime(2026, 7, 21, 12, 0, tzinfo=JST)
    opened = record_result(
        load_portfolio_ledger(FIXTURE),
        proposal_ref=PROPOSAL,
        status="open",
        occurred_at=_at(9),
        ticker="1234",
        quantity=200,
        sector="サービス業",
        price_guard_yen=Decimal("900"),
        expires_at=expiry,
        now=now,
    )
    reservation = next(
        item
        for item in reconcile_portfolio(opened.document).active_reservations
        if item.ticker == "1234"
    )
    partial = record_result(
        opened.document,
        proposal_ref=PROPOSAL,
        status="filled",
        occurred_at=_at(10),
        ticker="1234",
        quantity=100,
        price_yen=Decimal("890"),
        reservation_id=reservation.reservation_id,
        now=now,
    )
    partial_state = replay_events_through(partial.document.events, partial.document.as_of)
    assert (
        next(
            item
            for item in reservation_snapshots(partial_state)
            if item.reservation_id == reservation.reservation_id
        ).remaining_quantity
        == 100
    )
    expired = record_result(
        partial.document,
        proposal_ref=PROPOSAL,
        status="expired",
        occurred_at=expiry,
        reservation_id=reservation.reservation_id,
        now=now,
    )
    state = replay_events_through(expired.document.events, expired.document.as_of)
    assert all(
        item.reservation_id != reservation.reservation_id for item in reservation_snapshots(state)
    )
    assert sum(lot.quantity for lot in state.lots["1234"]) == 100


def test_cancelled_report_at_expiry_must_use_expired_status() -> None:
    with pytest.raises(ResultRecordingError, match="must use expired reason"):
        record_result(
            load_portfolio_ledger(FIXTURE),
            proposal_ref=PROPOSAL,
            status="cancelled",
            occurred_at=datetime(2026, 7, 31, 15, 30, tzinfo=JST),
            reservation_id="reservation-8929-pending",
            now=datetime(2026, 8, 1, 12, 0, tzinfo=JST),
        )


def test_filled_without_reservation_lists_missing_human_facts() -> None:
    with pytest.raises(ResultRecordingError, match="requires approved_at"):
        record_result(
            load_portfolio_ledger(FIXTURE),
            proposal_ref=PROPOSAL,
            status="filled",
            occurred_at=_at(10),
            ticker="2331",
            quantity=100,
            price_yen=Decimal("1090"),
            now=_at(12),
        )


def test_result_rejects_a_non_issue_reference() -> None:
    with pytest.raises(ResultRecordingError, match="HTTPS GitHub Issue URL"):
        record_result(
            load_portfolio_ledger(FIXTURE),
            proposal_ref="human-said-so",
            status="cancelled",
            occurred_at=_at(10),
            reservation_id="reservation-8929-pending",
            now=_at(12),
        )


def test_cli_writes_a_draft_without_mutating_the_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ledger.yaml"
    ledger.write_bytes(FIXTURE.read_bytes())
    before = ledger.read_bytes()
    output = tmp_path / "patched.yaml"
    exit_code = main(
        [
            "record-result",
            "--root",
            str(tmp_path),
            "--ledger",
            "ledger.yaml",
            "--proposal-ref",
            PROPOSAL,
            "--status",
            "cancelled",
            "--occurred-at",
            _at(10).isoformat(),
            "--reservation-id",
            "reservation-8929-pending",
            "--out",
            "patched.yaml",
        ],
        now=_at(12),
    )
    assert exit_code == 0
    assert ledger.read_bytes() == before
    assert yaml.safe_load(capsys.readouterr().out)["status"] == "draft_created"
    assert load_portfolio_ledger(output).events[-1].type == "release"


def test_cli_writes_human_confirmed_expiry_draft(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ledger.yaml"
    ledger.write_bytes(FIXTURE.read_bytes())
    occurred_at = datetime(2026, 7, 31, 15, 30, tzinfo=JST)
    argv = [
        "record-result",
        "--root",
        str(tmp_path),
        "--ledger",
        "ledger.yaml",
        "--proposal-ref",
        PROPOSAL,
        "--status",
        "expired",
        "--occurred-at",
        occurred_at.isoformat(),
        "--reservation-id",
        "reservation-8929-pending",
        "--out",
        "expired.yaml",
    ]

    assert main(argv, now=datetime(2026, 8, 1, 12, 0, tzinfo=JST)) == 0
    first = yaml.safe_load(capsys.readouterr().out)
    assert first["status"] == "draft_created"
    draft = load_portfolio_ledger(tmp_path / "expired.yaml")
    assert draft.events[-1].type == "release"
    assert draft.events[-1].reason == "expired"

    ledger.write_bytes((tmp_path / "expired.yaml").read_bytes())
    assert main(argv, now=datetime(2026, 8, 1, 12, 0, tzinfo=JST)) == 0
    assert yaml.safe_load(capsys.readouterr().out)["status"] == "no_change"


def test_cli_hashes_the_exact_ledger_bytes_used_for_the_draft(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "ledger.yaml"
    source = FIXTURE.read_bytes()
    ledger.write_bytes(source)
    original_write = position_cli._write_yaml_exclusive

    def mutate_source_then_write(path: Path, payload: object) -> None:
        ledger.write_bytes(source + b"\n")
        original_write(path, payload)

    monkeypatch.setattr(position_cli, "_write_yaml_exclusive", mutate_source_then_write)
    exit_code = main(
        [
            "record-result",
            "--root",
            str(tmp_path),
            "--ledger",
            "ledger.yaml",
            "--proposal-ref",
            PROPOSAL,
            "--status",
            "cancelled",
            "--occurred-at",
            _at(10).isoformat(),
            "--reservation-id",
            "reservation-8929-pending",
            "--out",
            "draft.yaml",
        ],
        now=_at(12),
    )
    assert exit_code == 0
    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload["source_ledger_sha256"] == hashlib.sha256(source).hexdigest()
    assert payload["source_ledger_sha256"] != hashlib.sha256(ledger.read_bytes()).hexdigest()


def test_cli_nochange_does_not_require_a_fresh_output_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ledger.yaml"
    ledger.write_bytes(FIXTURE.read_bytes())
    argv = [
        "record-result",
        "--root",
        str(tmp_path),
        "--ledger",
        "ledger.yaml",
        "--proposal-ref",
        PROPOSAL,
        "--status",
        "cancelled",
        "--occurred-at",
        _at(10).isoformat(),
        "--reservation-id",
        "reservation-8929-pending",
        "--out",
        "draft.yaml",
    ]
    assert main(argv, now=_at(12)) == 0
    capsys.readouterr()
    ledger.write_bytes(tmp_path.joinpath("draft.yaml").read_bytes())
    assert main(argv, now=_at(12)) == 0
    assert yaml.safe_load(capsys.readouterr().out)["status"] == "no_change"


def test_result_rejects_future_human_times() -> None:
    ledger = load_portfolio_ledger(FIXTURE)
    with pytest.raises(ResultRecordingError, match="occurred_at must not be in the future"):
        record_result(
            ledger,
            proposal_ref=PROPOSAL,
            status="cancelled",
            occurred_at=_at(13),
            reservation_id="reservation-8929-pending",
            now=_at(12),
        )
    with pytest.raises(ResultRecordingError, match="approved_at must not be in the future"):
        record_result(
            ledger,
            proposal_ref=PROPOSAL,
            status="filled",
            occurred_at=_at(11),
            ticker="1234",
            quantity=100,
            price_yen=Decimal("900"),
            approved_at=_at(13),
            price_guard_yen=Decimal("1000"),
            expires_at=datetime(2026, 7, 20, 15, 30, tzinfo=JST),
            sector="サービス業",
            now=_at(12),
        )


def test_corrected_fill_is_a_conflict_not_a_silent_noop() -> None:
    opened = record_result(
        load_portfolio_ledger(FIXTURE),
        proposal_ref=PROPOSAL,
        status="open",
        occurred_at=_at(9),
        ticker="2331",
        quantity=100,
        sector="サービス業",
        price_guard_yen=Decimal("1100"),
        expires_at=datetime(2026, 7, 20, 15, 30, tzinfo=JST),
        now=_at(12),
    )
    reservation_id = next(
        item.reservation_id
        for item in reconcile_portfolio(opened.document).active_reservations
        if item.ticker == "2331"
    )
    filled = record_result(
        opened.document,
        proposal_ref=PROPOSAL,
        status="filled",
        occurred_at=_at(10),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1090"),
        reservation_id=reservation_id,
        now=_at(12),
    )
    with pytest.raises(ResultRecordingError, match="conflicting human report"):
        record_result(
            filled.document,
            proposal_ref=PROPOSAL,
            status="filled",
            occurred_at=_at(10),
            ticker="2331",
            quantity=100,
            price_yen=Decimal("1080"),
            reservation_id=reservation_id,
            now=_at(12),
        )


def test_fill_cannot_replace_the_reservation_proposal_reference() -> None:
    opened = record_result(
        load_portfolio_ledger(FIXTURE),
        proposal_ref=PROPOSAL,
        status="open",
        occurred_at=_at(9),
        ticker="2331",
        quantity=100,
        sector="サービス業",
        price_guard_yen=Decimal("1100"),
        expires_at=datetime(2026, 7, 20, 15, 30, tzinfo=JST),
        now=_at(12),
    )
    reservation_id = next(
        item.reservation_id
        for item in reconcile_portfolio(opened.document).active_reservations
        if item.ticker == "2331"
    )
    with pytest.raises(ResultRecordingError, match="does not match the active reservation"):
        record_result(
            opened.document,
            proposal_ref="https://github.com/koumatsumoto/baibai-loop/issues/361",
            status="filled",
            occurred_at=_at(10),
            ticker="2331",
            quantity=100,
            price_yen=Decimal("1090"),
            reservation_id=reservation_id,
            now=_at(12),
        )


def test_cli_rejects_output_outside_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ledger.yaml"
    ledger.write_bytes(FIXTURE.read_bytes())
    exit_code = main(
        [
            "record-result",
            "--root",
            str(tmp_path),
            "--ledger",
            "ledger.yaml",
            "--proposal-ref",
            PROPOSAL,
            "--status",
            "cancelled",
            "--occurred-at",
            _at(10).isoformat(),
            "--reservation-id",
            "reservation-8929-pending",
            "--out",
            "../escaped.yaml",
        ],
        now=_at(12),
    )
    assert exit_code == 2
    assert "relative path below --root" in capsys.readouterr().err


def test_direct_fill_inserts_approval_before_newer_existing_events() -> None:
    result = record_result(
        load_portfolio_ledger(FIXTURE),
        proposal_ref=PROPOSAL,
        status="filled",
        occurred_at=_at(10),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1090"),
        approved_at=_at(9),
        price_guard_yen=Decimal("1100"),
        expires_at=datetime(2026, 7, 20, 15, 30, tzinfo=JST),
        now=_at(12),
    )
    times = [event.occurred_at for event in result.document.events]
    assert times == sorted(times)
    holding = next(
        item for item in reconcile_portfolio(result.document).holdings if item.ticker == "2331"
    )
    assert holding.quantity == 300


def test_cli_rejects_symlink_draft_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tmp_path.joinpath("ledger.yaml").write_bytes(FIXTURE.read_bytes())
    tmp_path.joinpath("draft.yaml").symlink_to(tmp_path / "outside.yaml")
    exit_code = main(
        [
            "record-result",
            "--root",
            str(tmp_path),
            "--ledger",
            "ledger.yaml",
            "--proposal-ref",
            PROPOSAL,
            "--status",
            "cancelled",
            "--occurred-at",
            _at(10).isoformat(),
            "--reservation-id",
            "reservation-8929-pending",
            "--out",
            "draft.yaml",
        ],
        now=_at(12),
    )
    assert exit_code == 2
    assert "must not traverse a symlink" in capsys.readouterr().err
    assert not tmp_path.joinpath("outside.yaml").exists()
