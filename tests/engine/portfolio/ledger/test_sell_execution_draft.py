from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger, portfolio_snapshot

from baibai_engine.cli.portfolio import main
from baibai_engine.portfolio.ledger.drafts import (
    apply_draft,
    build_sell_execution_draft,
    load_draft,
)
from baibai_engine.portfolio.ledger.events import (
    ExecutionEvent,
    PortfolioLedgerError,
)
from baibai_engine.portfolio.ledger.store import LedgerStoreService

ROOT = Path(__file__).resolve().parents[4]
LEDGER = ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"
JST = ZoneInfo("Asia/Tokyo")
OCCURRED_AT = datetime(2026, 7, 12, 10, 0, tzinfo=JST)
DECISION_REF = "position-review-20260711-2331-position-2331"


def _seed(db: Path) -> LedgerStoreService:
    source = load_portfolio_ledger(LEDGER)
    seed_ledger(db, source)
    return LedgerStoreService(db)


def _holding(service: LedgerStoreService, ticker: str) -> object:
    return next(
        item for item in portfolio_snapshot(service.load()).holdings if item.ticker == ticker
    )


@pytest.mark.parametrize(
    ("flag", "value", "report_clock", "accepted"),
    [
        ("--occurred-at", "2026-07-12T10:10:00+09:00", "2026-07-12T10:00:00+09:00", False),
        ("--occurred-at", "2026-07-12T00:00:00+09:00", "2026-07-11T14:59:59.999999Z", False),
        ("--occurred-at", "2026-07-12T00:00:00+09:00", "2026-07-11T15:00:00Z", True),
        ("--occurred-at", "2026-07-11T15:00:00Z", "2026-07-12T00:00:00.000001+09:00", True),
        ("--occurred-on", "2026-07-12", "2026-07-11T14:59:59.999999Z", False),
        ("--occurred-on", "2026-07-12", "2026-07-11T15:00:00Z", True),
        ("--occurred-on", "2026-07-11", "2026-07-11T15:00:00Z", True),
    ],
)
def test_new_sell_cannot_be_reported_before_its_instant_or_tokyo_day(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flag: str,
    value: str,
    report_clock: str,
    accepted: bool,
) -> None:
    db, out = tmp_path / "app.sqlite", tmp_path / "draft.yaml"
    _seed(db)
    before = db.read_bytes()
    result = main(
        [
            "sell-execution-draft",
            "--db",
            str(db),
            "--ticker",
            "2331",
            "--quantity",
            "100",
            "--price-yen",
            "1100",
            flag,
            value,
            "--out",
            str(out),
        ],
        now=datetime.fromisoformat(report_clock),
    )
    assert result == (0 if accepted else 2)
    assert out.exists() is accepted
    assert db.read_bytes() == before
    if not accepted:
        assert "must not be in the future" in capsys.readouterr().err


def test_date_only_sell_does_not_claim_latest_saved_replay_key_is_its_fill_time(tmp_path: Path):
    service = _seed(tmp_path / "app.sqlite")
    report = dict(ticker="2331", quantity=100, price_yen=Decimal("1100"))
    previous = build_sell_execution_draft(
        service, **report, occurred_at=OCCURRED_AT, now=OCCURRED_AT
    )
    assert previous is not None
    apply_draft(service, previous, human_confirmed=True)
    reported_earlier_that_day = OCCURRED_AT - timedelta(hours=1)
    dated = build_sell_execution_draft(
        service,
        **report,
        occurred_on=OCCURRED_AT.date(),
        now=reported_earlier_that_day,
    )
    assert dated is not None
    assert dated.replacement.events[:-1] == dated.source.events
    new = dated.replacement.events[-1]
    assert new.occurred_on == OCCURRED_AT.date()
    assert new.occurred_at == OCCURRED_AT > reported_earlier_that_day


@pytest.mark.parametrize(
    ("fill_fold", "fill_minute", "report_fold", "report_minute", "accepted"),
    [
        (0, 30, 0, 30, True),
        (1, 30, 1, 30, True),
        (1, 30, 0, 30, False),
        (0, 30, 1, 30, True),
        (1, 15, 0, 45, False),
        (0, 45, 1, 15, True),
    ],
)
def test_sell_report_compares_instants_through_a_repeated_clock_hour(
    tmp_path: Path,
    fill_fold: int,
    fill_minute: int,
    report_fold: int,
    report_minute: int,
    accepted: bool,
) -> None:
    service = _seed(tmp_path / "app.sqlite")
    repeated = datetime(2026, 11, 1, 1, tzinfo=ZoneInfo("America/New_York"))
    fill = repeated.replace(minute=fill_minute, fold=fill_fold)
    reported = repeated.replace(minute=report_minute, fold=report_fold)
    trade = dict(ticker="2331", quantity=100, price_yen=Decimal("1100"))
    if accepted:
        assert build_sell_execution_draft(service, **trade, occurred_at=fill, now=reported)
    else:
        with pytest.raises(ValueError, match="occurred_at must not be in the future"):
            build_sell_execution_draft(service, **trade, occurred_at=fill, now=reported)


def test_sell_over_current_holding_is_rejected_fail_closed(tmp_path: Path) -> None:
    service = _seed(tmp_path / "app.sqlite")
    with pytest.raises(PortfolioLedgerError, match="exceeds repository holding for 2331"):
        build_sell_execution_draft(
            service,
            occurred_at=OCCURRED_AT,
            ticker="2331",
            quantity=300,
            price_yen=Decimal("1100"),
        )


def test_sell_draft_apply_reduces_holding_and_realizes_fifo_pnl(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "app.sqlite"
    service = _seed(db)
    before = portfolio_snapshot(service.load())
    before_cash = before.available_cash_yen
    before_holding = _holding(service, "2331")
    assert before_holding.quantity == 200
    assert before_holding.deployed_cost_yen == 203_000

    assert (
        main(
            [
                "sell-execution-draft",
                "--root",
                str(tmp_path),
                "--db",
                str(db),
                "--ticker",
                "2331",
                "--quantity",
                "100",
                "--price-yen",
                "1100",
                "--occurred-at",
                OCCURRED_AT.isoformat(),
                "--decision-reference",
                DECISION_REF,
                "--out",
                "sell-draft.yaml",
            ]
        )
        == 0
    )
    output = yaml.safe_load(capsys.readouterr().out)
    assert output["status"] == "draft_created"
    assert output["ticker"] == "2331"

    draft_path = tmp_path / "sell-draft.yaml"
    draft = load_draft(draft_path)
    assert draft.kind == "sell-execution"
    # Draft generation never touches the canonical DB.
    assert draft.expected_head == service.load_with_head()[1]
    assert _holding(service, "2331").quantity == 200

    assert main(["apply-draft", str(draft_path), "--db", str(db), "--confirmed"]) == 0

    after_document = service.load()
    after = portfolio_snapshot(after_document)
    after_holding = _holding(service, "2331")

    proceeds = 100 * 1100
    consumed_cost = before_holding.deployed_cost_yen - after_holding.deployed_cost_yen
    realized_gross_pnl = proceeds - consumed_cost

    assert after_holding.quantity == 100
    # FIFO consumes the oldest 100@1040 lot, leaving 100@990.
    assert after_holding.deployed_cost_yen == 99_000
    assert consumed_cost == 104_000
    assert realized_gross_pnl == 6_000
    assert after.realized_gross_pnl_yen == 6_000
    assert after.available_cash_yen == before_cash + proceeds

    sell = next(
        event
        for event in after_document.events
        if event.type == "execution" and event.event_id.startswith("human-sell-")
    )
    assert sell.side == "sell"
    assert sell.reservation_id is None
    assert sell.decision_reference == DECISION_REF


def test_sell_quantity_not_multiple_of_board_lot_is_rejected(tmp_path: Path) -> None:
    service = _seed(tmp_path / "app.sqlite")
    with pytest.raises(PortfolioLedgerError, match="multiple of board_lot"):
        build_sell_execution_draft(
            service,
            occurred_at=OCCURRED_AT,
            ticker="2331",
            quantity=150,
            price_yen=Decimal("1100"),
        )


@pytest.mark.parametrize(("fees_yen", "tax_yen"), [(0, 0), (100, 200)])
def test_date_only_sell_keeps_reported_date_without_claiming_fill_time(
    tmp_path: Path, fees_yen: int, tax_yen: int
) -> None:
    db = tmp_path / "app.sqlite"
    service = _seed(db)
    before = portfolio_snapshot(service.load())
    assert (
        main(
            [
                "sell-execution-draft",
                "--root",
                str(tmp_path),
                "--db",
                str(db),
                "--ticker",
                "2331",
                "--quantity",
                "100",
                "--price-yen",
                "1100",
                "--fees-yen",
                str(fees_yen),
                "--tax-yen",
                str(tax_yen),
                "--occurred-on",
                "2026-07-12",
                "--decision-reference",
                DECISION_REF,
                "--out",
                "date-only-sell.yaml",
            ]
        )
        == 0
    )
    draft = load_draft(tmp_path / "date-only-sell.yaml")
    source_ids = {item.event_id for item in draft.source.events}
    additions = [event for event in draft.replacement.events if event.event_id not in source_ids]
    assert {event.type for event in additions} == {"execution", "cost", "tax_confirmed"}
    assert all(event.occurred_on == date(2026, 7, 12) for event in additions)
    assert all(event.occurred_at.isoformat() == "2026-07-12T00:00:00+09:00" for event in additions)
    assert (
        main(["apply-draft", str(tmp_path / "date-only-sell.yaml"), "--db", str(db), "--confirmed"])
        == 0
    )
    saved = [event for event in service.load().events if event.event_id not in source_ids]
    assert all(event.occurred_on == date(2026, 7, 12) for event in saved)
    assert {event.type: event.amount_yen for event in saved if event.type != "execution"} == {
        "cost": fees_yen,
        "tax_confirmed": tax_yen,
    }
    expected_cash = before.available_cash_yen + 110_000 - fees_yen - tax_yen
    assert portfolio_snapshot(service.load()).available_cash_yen == expected_cash
    assert portfolio_snapshot(service.load()).realized_gross_pnl_yen == 6_000


def test_sell_cli_passes_execution_id_and_reports_retry_as_no_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "app.sqlite"
    service = _seed(db)
    args = [
        "sell-execution-draft",
        "--root",
        str(tmp_path),
        "--db",
        str(db),
        "--ticker",
        "2331",
        "--quantity",
        "100",
        "--price-yen",
        "1100",
        "--occurred-on",
        "2026-07-12",
        "--execution-id",
        "broker-order-s/fill-1",
    ]
    assert main([*args, "--out", "sell.yaml"], now=OCCURRED_AT) == 0
    assert yaml.safe_load(capsys.readouterr().out)["status"] == "draft_created"
    draft = load_draft(tmp_path / "sell.yaml")
    assert (
        next(
            item.execution_id
            for item in draft.replacement.events
            if item.event_id not in {event.event_id for event in draft.source.events}
        )
        == "broker-order-s/fill-1"
    )
    apply_draft(service, draft, human_confirmed=True)
    assert main([*args, "--out", "retry.yaml"], now=OCCURRED_AT - timedelta(days=1)) == 0
    assert yaml.safe_load(capsys.readouterr().out)["status"] == "no_change"
    assert not (tmp_path / "retry.yaml").exists()


@pytest.mark.parametrize(
    ("first_date_only", "retry_date_only"),
    [(True, True), (False, False), (True, False), (False, True)],
)
def test_new_id_cannot_double_book_a_saved_idless_sale_and_its_charges(
    tmp_path, capsys, first_date_only, retry_date_only
):
    db = tmp_path / "app.sqlite"
    service = _seed(db)
    opening = portfolio_snapshot(service.load())
    before = db.read_bytes()
    # The first timestamp's local date differs from the report's JST date.
    old_offset = timezone(timedelta(hours=-3, minutes=-30))
    stored = OCCURRED_AT.astimezone(old_offset)
    assert stored.date() != OCCURRED_AT.date()
    day = ["--occurred-on", OCCURRED_AT.date().isoformat()]
    first_at = day if first_date_only else ["--occurred-at", stored.isoformat()]
    retry_at = (
        day if retry_date_only else ["--occurred-at", OCCURRED_AT.astimezone(UTC).isoformat()]
    )
    args = [
        "sell-execution-draft",
        "--root",
        str(tmp_path),
        "--db",
        str(db),
        "--ticker",
        "2331",
        "--quantity",
        "100",
    ]
    details = [
        "--fees-yen",
        "500",
        "--tax-yen",
        "900",
        "--decision-reference",
        DECISION_REF,
    ]
    assert (
        main(
            [*args, *first_at, *details, "--price-yen", "1100.0000", "--out", "first.yaml"],
            now=OCCURRED_AT,
        )
        == 0
    )
    assert yaml.safe_load(capsys.readouterr().out)["status"] == "draft_created"
    assert db.read_bytes() == before
    apply_draft(service, load_draft(tmp_path / "first.yaml"), human_confirmed=True)
    before = db.read_bytes()
    saved, head = service.load_with_head()
    after_first = portfolio_snapshot(saved)
    assert _holding(service, "2331").quantity == 100
    assert after_first.available_cash_yen == opening.available_cash_yen + 110_000 - 500 - 900
    assert after_first.realized_gross_pnl_yen == 6_000
    assert after_first.confirmed_cost_yen == opening.confirmed_cost_yen + 500
    assert after_first.confirmed_tax_yen == opening.confirmed_tax_yen + 900

    if first_date_only == retry_date_only:
        exact_retry = [*args, *retry_at, *details, "--price-yen", "1100", "--out", "unchanged.yaml"]
        assert main(exact_retry, now=OCCURRED_AT) == 0
        assert yaml.safe_load(capsys.readouterr().out)["status"] == "no_change"
        assert not (tmp_path / "unchanged.yaml").exists()
    arbitrary_id = [*args, *retry_at, "--price-yen", "1100", "--execution-id", "new-id"]
    # Omitting or amending fee, tax or Proposal metadata does not identify a separate sale.
    for report in (
        details,
        ["--fees-yen", "200", "--decision-reference", "updated-ref"],
    ):
        assert main([*arbitrary_id, *report, "--out", "duplicate.yaml"], now=OCCURRED_AT) == 2
        output = capsys.readouterr()
        assert output.out == ""
        assert "sale previously reported without a broker ID" in output.err
        assert not (tmp_path / "duplicate.yaml").exists()
        assert service.load_with_head() == (saved, head)
        assert db.read_bytes() == before


@pytest.mark.parametrize("date_only", [False, True])
def test_two_confirmed_sale_ids_can_have_equal_trade_terms(tmp_path, date_only):
    db = tmp_path / "app.sqlite"
    service = _seed(db)
    opening = portfolio_snapshot(service.load())
    report = dict(
        ticker="2331",
        **({"occurred_on": OCCURRED_AT.date()} if date_only else {"occurred_at": OCCURRED_AT}),
        quantity=100,
        price_yen=Decimal("1100"),
        fees_yen=500,
        tax_yen=900,
        now=OCCURRED_AT,
    )
    before = db.read_bytes()
    preview = build_sell_execution_draft(service, **report)
    original = preview.replacement.events[-3]
    assert isinstance(original, ExecutionEvent)
    # A real broker ID identical to the text of the hypothetical generated ID
    # does not make the saved explicit sale a fallback.
    for broker_id in (original.execution_id, "other-broker-sale"):
        draft = build_sell_execution_draft(service, **report, execution_id=broker_id)
        assert draft is not None
        sale = draft.replacement.events[-3]
        assert isinstance(sale, ExecutionEvent)
        assert sale.event_id != original.event_id
        assert db.read_bytes() == before
        apply_draft(service, draft, human_confirmed=True)
        before = db.read_bytes()
        assert build_sell_execution_draft(service, **report, execution_id=broker_id) is None
        assert db.read_bytes() == before

    closing = portfolio_snapshot(service.load())
    assert all(item.ticker != "2331" for item in closing.holdings)
    assert closing.available_cash_yen == opening.available_cash_yen + 220_000 - 1_000 - 1_800
    assert closing.realized_gross_pnl_yen == 17_000
    assert closing.confirmed_cost_yen == opening.confirmed_cost_yen + 1_000
    assert closing.confirmed_tax_yen == opening.confirmed_tax_yen + 1_800


def test_sell_fees_and_tax_over_proceeds_and_cash_are_rejected(tmp_path: Path) -> None:
    service = _seed(tmp_path / "app.sqlite")
    with pytest.raises(PortfolioLedgerError, match="insufficient available cash"):
        build_sell_execution_draft(
            service,
            occurred_at=OCCURRED_AT,
            ticker="2331",
            quantity=100,
            price_yen=Decimal("1100"),
            fees_yen=11_000_000,
        )


@pytest.mark.parametrize("retry_at", [OCCURRED_AT, OCCURRED_AT.astimezone(UTC)])
def test_identical_sell_parameters_after_apply_are_no_change(
    tmp_path: Path, retry_at: datetime
) -> None:
    service = _seed(tmp_path / "app.sqlite")
    first = build_sell_execution_draft(
        service,
        occurred_at=OCCURRED_AT,
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1100"),
        now=OCCURRED_AT,
    )
    apply_draft(service, first, human_confirmed=True)
    assert (
        build_sell_execution_draft(
            service,
            occurred_at=retry_at,
            ticker="2331",
            quantity=100,
            price_yen=Decimal("1100"),
            now=OCCURRED_AT - timedelta(microseconds=1),
        )
        is None
    )
    assert _holding(service, "2331").quantity == 100
    with pytest.raises(ValueError, match="conflicting human report"):
        build_sell_execution_draft(
            service,
            occurred_at=retry_at,
            ticker="2331",
            quantity=100,
            price_yen=Decimal("1101"),
            now=OCCURRED_AT - timedelta(microseconds=1),
        )


def test_cli_rejects_zero_quantity_zero_price_and_negative_fees(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    _seed(db)
    base = [
        "sell-execution-draft",
        "--root",
        str(tmp_path),
        "--db",
        str(db),
        "--ticker",
        "2331",
        "--occurred-at",
        OCCURRED_AT.isoformat(),
    ]
    assert main([*base, "--quantity", "0", "--price-yen", "1100", "--out", "q0.yaml"]) == 2
    with pytest.raises(SystemExit) as excinfo:
        main([*base, "--quantity", "100", "--price-yen", "0", "--out", "p0.yaml"])
    assert excinfo.value.code == 2
    assert (
        main(
            [
                *base,
                "--quantity",
                "100",
                "--price-yen",
                "1100",
                "--fees-yen",
                "-500",
                "--out",
                "f.yaml",
            ]
        )
        == 2
    )


def test_sell_draft_records_fees_and_tax_as_cost_and_tax_events(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    service = _seed(db)
    before = portfolio_snapshot(service.load())

    draft = build_sell_execution_draft(
        service,
        occurred_at=OCCURRED_AT,
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1100"),
        fees_yen=500,
        tax_yen=900,
        decision_reference=DECISION_REF,
    )
    apply_draft(service, draft, human_confirmed=True)

    after_document = service.load()
    after = portfolio_snapshot(after_document)

    assert after.confirmed_cost_yen == before.confirmed_cost_yen + 500
    assert after.confirmed_tax_yen == before.confirmed_tax_yen + 900
    assert after.available_cash_yen == before.available_cash_yen + 100 * 1100 - 500 - 900
    assert after.realized_gross_pnl_yen == 6_000

    fee = next(
        event for event in after_document.events if event.event_id.startswith("human-sell-fee-")
    )
    tax = next(
        event for event in after_document.events if event.event_id.startswith("human-sell-tax-")
    )
    assert fee.type == "cost"
    assert fee.cost_kind == "commission"
    assert tax.type == "tax_confirmed"
    assert tax.tax_kind == "capital_gain"
