from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger

from baibai_engine.cli.portfolio import main as position_main
from baibai_engine.portfolio.ledger.drafts import LedgerDraft, apply_draft, build_event_draft
from baibai_engine.portfolio.ledger.events import (
    ContributionEvent,
    CostEvent,
    replay_events_through,
)
from baibai_engine.portfolio.ledger.read import LedgerConflictError
from baibai_engine.portfolio.ledger.store import LedgerStoreService
from baibai_engine.primitives.yaml import safe_load

ROOT = Path(__file__).resolve().parents[4]
LEDGER = ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"
EVENT_CLOCK = datetime.fromisoformat("2026-07-18T00:00:00+00:00")


def _seeded_db(tmp_path: Path) -> Path:
    db = tmp_path / "app.sqlite"
    source = load_portfolio_ledger(LEDGER)
    seed_ledger(db, source)
    return db


def test_event_draft_requires_confirmation_and_rejects_stale_apply(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)
    service = LedgerStoreService(db)
    first = build_event_draft(
        service,
        ContributionEvent.model_validate(
            {
                "event_id": "human-contribution-test-1",
                "type": "contribution",
                "occurred_at": "2026-07-18T09:00:00+09:00",
                "amount_yen": 10_000,
            }
        ),
    )
    second = build_event_draft(
        service,
        ContributionEvent.model_validate(
            {
                "event_id": "human-contribution-test-2",
                "type": "contribution",
                "occurred_at": "2026-07-18T09:01:00+09:00",
                "amount_yen": 20_000,
            }
        ),
    )

    with pytest.raises(ValueError, match="human confirmation"):
        apply_draft(service, first, human_confirmed=False)
    applied = apply_draft(service, first, human_confirmed=True)
    assert applied.event_ids == ("human-contribution-test-1",)
    with pytest.raises(LedgerConflictError, match="stale"):
        apply_draft(service, second, human_confirmed=True)


@pytest.mark.parametrize("delay", [timedelta(), timedelta(seconds=1)])
def test_late_event_draft_preserves_saved_order_and_appends_a_new_ordinal(
    tmp_path: Path, delay: timedelta
) -> None:
    db = _seeded_db(tmp_path)
    service = LedgerStoreService(db)
    source, head = service.load_with_head()
    event = ContributionEvent(
        event_id="aaa-reported-later",
        type="contribution",
        occurred_at=source.events[2].occurred_at.astimezone(UTC) + delay,
        amount_yen=10_000,
    )
    draft = build_event_draft(service, event)
    assert draft.replacement.events == (*source.events[:3], event, *source.events[3:])
    assert draft.replacement.as_of == source.as_of
    assert service.load_with_head() == (source, head)

    assert apply_draft(service, draft, human_confirmed=True).append_head == head + 1
    assert service.load() == draft.replacement
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute(
            "SELECT append_seq, same_instant_order FROM ledger_event WHERE event_id = ?",
            (event.event_id,),
        ).fetchone() == (head + 1, int(not delay))


def test_record_broker_fact_apply_preserves_unreported_expired_order(
    tmp_path: Path,
) -> None:
    db = _seeded_db(tmp_path)
    service = LedgerStoreService(db)
    source = service.load()
    draft = LedgerDraft(
        kind="broker-fact",
        expected_head=service.load_with_head()[1],
        source=source,
        replacement=source.model_copy(
            update={"as_of": datetime.fromisoformat("2026-08-01T09:00:00+09:00")}
        ),
    )

    apply_draft(service, draft, human_confirmed=True)
    assert service.load().events == source.events
    assert service.load().as_of == draft.replacement.as_of


@pytest.mark.parametrize(
    ("event_type", "extra"),
    [
        ("contribution", []),
        ("withdrawal", []),
        ("income", ["--ticker", "2331", "--kind", "dividend"]),
        ("cost", ["--ticker", "2331", "--kind", "commission"]),
        ("tax_confirmed", ["--ticker", "2331", "--kind", "capital_gain"]),
    ],
)
@pytest.mark.parametrize(
    ("occurrence_option", "occurrence"),
    [
        ("--occurred-at", "2026-07-18T09:00:00+09:00"),
        ("--occurred-on", "2026-07-18"),
    ],
)
@pytest.mark.parametrize("future", [False, True])
def test_event_draft_cli_writes_only_occurred_cash_facts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    event_type: str,
    extra: list[str],
    occurrence_option: str,
    occurrence: str,
    future: bool,
) -> None:
    """Drive the command, not the model it wraps.

    The CLI parses `--occurred-at` into a datetime before the event model sees it,
    so a model that only accepts text rejects every draft the runbook asks for
    while the model-level tests keep passing.
    """
    db = _seeded_db(tmp_path)
    before = db.read_bytes()
    out = tmp_path / f"{event_type}-draft.yaml"
    if future:
        occurrence = (
            "2026-07-19" if occurrence_option == "--occurred-on" else "2026-07-18T09:00:01+09:00"
        )

    code = position_main(
        [
            "event-draft",
            "--type",
            event_type,
            "--event-id",
            f"human-{event_type}-cli",
            occurrence_option,
            occurrence,
            "--amount-yen",
            "10000",
            "--db",
            str(db),
            "--out",
            str(out),
            *extra,
        ],
        now=EVENT_CLOCK,
    )

    observed = capsys.readouterr()
    if future:
        assert code == 2
        assert "must not be in the future" in observed.err
        assert observed.out == ""
        assert not out.exists()
    else:
        assert code == 0
        assert observed.out.startswith("status: draft_created")
        draft = safe_load(out.read_text(encoding="utf-8"))
        assert draft["kind"] == "event"
    assert db.read_bytes() == before


@pytest.mark.parametrize("draft_kind", ["event", "broker-fact"])
@pytest.mark.parametrize("date_only", [False, True])
def test_apply_rejects_future_cash_even_if_a_prepared_draft_is_edited(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], draft_kind: str, date_only: bool
) -> None:
    db = _seeded_db(tmp_path)
    service = LedgerStoreService(db)
    before = db.read_bytes()
    source, head = service.load_with_head()
    out = tmp_path / "cash.yaml"
    event_id = "confirmed-other-cash-flow"
    # JST July 18 begins while the UTC clock still says July 17.
    now = datetime.fromisoformat("2026-07-17T15:10:00+00:00")
    prepare = [
        "event-draft",
        "--db",
        str(db),
        "--out",
        str(out),
        "--type",
        "contribution",
        "--event-id",
        event_id,
        "--amount-yen",
        "10000",
        "--occurred-on",
        "2026-07-18",
    ]
    assert position_main(prepare, now=now) == 0
    capsys.readouterr()
    authentic = out.read_bytes()
    authored = safe_load(authentic.decode("utf-8"))
    authored["kind"] = draft_kind
    event = next(e for e in authored["replacement"]["events"] if e["event_id"] == event_id)
    event["occurred_at"] = "2026-07-19T00:00:00+09:00"
    event["occurred_on"] = "2026-07-19" if date_only else None
    authored["replacement"]["as_of"] = event["occurred_at"]
    out.write_text(yaml.safe_dump(authored), encoding="utf-8")
    edited = out.read_bytes()
    apply = ["apply-draft", str(out), "--db", str(db), "--confirmed"]
    assert position_main(apply, now=now) == 2
    feedback = capsys.readouterr()
    assert feedback.out == ""
    assert "must not be in the future" in feedback.err
    assert out.read_bytes() == edited
    assert db.read_bytes() == before
    assert service.load_with_head() == (source, head)

    # The unmodified report is valid on its actual JST day and still applies once.
    out.write_bytes(authentic)
    assert position_main(apply, now=now) == 0
    assert "status: applied" in capsys.readouterr().out
    saved, new_head = service.load_with_head()
    assert new_head == head + 1
    assert any(e.event_id == event_id for e in saved.events)


def test_event_rejects_a_timezone_naive_instant() -> None:
    # The model is the last line for YAML-authored events, where argparse's own
    # timezone check is not in the path.
    with pytest.raises(ValidationError, match="must include a timezone"):
        ContributionEvent.model_validate(
            {
                "event_id": "human-contribution-naive",
                "type": "contribution",
                # DTZ001: the missing tzinfo is the input under test.
                "occurred_at": datetime(2026, 7, 18, 9, 0),  # noqa: DTZ001
                "amount_yen": 10_000,
            }
        )


def test_event_rejects_a_value_that_is_not_an_instant() -> None:
    with pytest.raises(ValidationError, match="must be an ISO datetime"):
        ContributionEvent.model_validate(
            {
                "event_id": "human-contribution-date-only",
                "type": "contribution",
                "occurred_at": date(2026, 7, 18),
                "amount_yen": 10_000,
            }
        )


def test_confirmed_zero_commission_is_recorded_without_changing_cash(tmp_path: Path) -> None:
    service = LedgerStoreService(_seeded_db(tmp_path))
    before = service.load()
    draft = build_event_draft(
        service,
        CostEvent.model_validate(
            {
                "event_id": "human-confirmed-zero-commission",
                "type": "cost",
                "occurred_at": "2026-07-18T00:00:00+09:00",
                "occurred_on": "2026-07-18",
                "ticker": "2331",
                "cost_kind": "commission",
                "amount_yen": 0,
            }
        ),
    )
    apply_draft(service, draft, human_confirmed=True)
    stored = service.load()
    assert len(stored.events) == len(before.events) + 1
    assert stored.events[-1].amount_yen == 0
    assert stored.events[:-1] == before.events
    previous = replay_events_through(before.events, before.as_of)
    current = replay_events_through(stored.events, stored.as_of)
    assert current.available_cash_yen == previous.available_cash_yen
    assert current.reserved_cash_yen == previous.reserved_cash_yen


@pytest.mark.parametrize("status", ["open", "filled", "cancelled", "direct-sale"])
@pytest.mark.parametrize("date_only", [False, True])
def test_apply_records_trade_fact_only_after_its_reported_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], status: str, date_only: bool
) -> None:
    """A draft prepared on a clock ahead of the ledger cannot book the future."""
    db = _seeded_db(tmp_path)
    service = LedgerStoreService(db)
    origin = ["--side", "sell", "--decision-reference", "human-reported-sale"]
    units = ["--ticker", "2331", "--quantity", "100"]
    new_order = ["--status", "open", *units, "--price-guard-yen", "1000", "--sector", "サービス業"]
    before_midnight = datetime.fromisoformat("2026-07-11T14:59:59Z")
    midnight = before_midnight + timedelta(seconds=1)
    if status in {"filled", "cancelled"}:
        prior = tmp_path / "prior.yaml"
        assert (
            position_main(
                [
                    "broker-fact-draft",
                    *origin,
                    *new_order,
                    "--occurred-at",
                    "2026-07-11T12:00:00+09:00",
                    "--db",
                    str(db),
                    "--out",
                    str(prior),
                ],
                now=before_midnight,
            )
            == 0
        )
        assert (
            position_main(
                ["apply-draft", str(prior), "--db", str(db), "--confirmed"], now=before_midnight
            )
            == 0
        )
        active = replay_events_through(service.load().events, before_midnight).active_reservations
        reservation_id = next(key for key, value in active.items() if value.side == "sell")
        args = [
            "broker-fact-draft",
            *origin,
            "--status",
            status,
            "--reservation-id",
            reservation_id,
        ]
        if status == "filled":
            args += [*units, "--price-yen", "1000"]
    elif status == "open":
        args = ["broker-fact-draft", *origin, *new_order]
    else:
        args = ["sell-execution-draft", *units, "--price-yen", "1000"]
    moment = (
        ["--occurred-on", "2026-07-12"] if date_only else ["--occurred-at", midnight.isoformat()]
    )
    draft = tmp_path / "fact.yaml"
    assert position_main([*args, *moment, "--db", str(db), "--out", str(draft)], now=midnight) == 0
    capsys.readouterr()
    before = service.load_with_head()
    original = db.read_bytes()
    apply = ["apply-draft", str(draft), "--db", str(db), "--confirmed"]
    assert position_main(apply, now=before_midnight) == 2
    assert "must not be in the future" in capsys.readouterr().err
    assert service.load_with_head() == before
    assert db.read_bytes() == original
    assert position_main(apply, now=midnight) == 0
    assert service.load_with_head()[1] == before[1] + 1
