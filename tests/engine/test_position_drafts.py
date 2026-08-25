from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.cli import main as position_main
from baibai_engine.position.drafts import LedgerDraft, apply_draft, build_event_draft
from baibai_engine.position.ledger import (
    ContributionEvent,
    PortfolioLedgerError,
)
from baibai_engine.position.store import LedgerConflictError, LedgerStoreService

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"


def _seeded_db(tmp_path: Path) -> Path:
    db = tmp_path / "app.sqlite"
    source = load_portfolio_ledger(LEDGER)
    seed_ledger(
        db,
        source.model_copy(
            update={
                "market_prices": tuple(
                    price.model_copy(update={"source_kind": "licensed_dataset"})
                    for price in source.market_prices
                )
            }
        ),
    )
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


def test_record_result_apply_still_rejects_an_unreleased_expired_reservation(
    tmp_path: Path,
) -> None:
    db = _seeded_db(tmp_path)
    service = LedgerStoreService(db)
    source = service.load()
    draft = LedgerDraft(
        kind="record-result",
        expected_head=service.append_head(),
        source=source,
        replacement=source.model_copy(
            update={"as_of": datetime.fromisoformat("2026-08-01T09:00:00+09:00")}
        ),
    )

    with pytest.raises(PortfolioLedgerError, match="expired reservations require"):
        apply_draft(service, draft, human_confirmed=True)


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
def test_event_draft_cli_writes_a_draft_for_every_event_type(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    event_type: str,
    extra: list[str],
) -> None:
    """Drive the command, not the model it wraps.

    The CLI parses `--occurred-at` into a datetime before the event model sees it,
    so a model that only accepts text rejects every draft the runbook asks for
    while the model-level tests keep passing.
    """
    db = _seeded_db(tmp_path)
    out = tmp_path / f"{event_type}-draft.yaml"

    code = position_main(
        [
            "event-draft",
            "--type",
            event_type,
            "--event-id",
            f"human-{event_type}-cli",
            "--occurred-at",
            "2026-07-18T09:00:00+09:00",
            "--amount-yen",
            "10000",
            "--db",
            str(db),
            "--out",
            str(out),
            *extra,
        ]
    )

    assert code == 0
    assert capsys.readouterr().out.startswith("status: draft_created")
    draft = safe_load(out.read_text(encoding="utf-8"))
    assert draft["kind"] == "event"


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
