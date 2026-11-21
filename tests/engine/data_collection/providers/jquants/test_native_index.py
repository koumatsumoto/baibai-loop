from datetime import UTC, date, datetime

from baibai_engine.data_collection.providers.jquants.native_index import (
    IncompleteReceipt,
    NativeMarketIndex,
    ReceiptEvidence,
    plan_required_days,
)


def test_required_days_distinguishes_nonempty_rule_and_unknown_request():
    clock = datetime(2026, 11, 4, tzinfo=UTC)
    empty, full, missing = (date(2026, 11, day) for day in (2, 3, 4))
    index = NativeMarketIndex(
        "margin_interest",
        (
            ReceiptEvidence("empty", "margin_interest", empty, empty, clock, 0),
            ReceiptEvidence("full", "margin_interest", full, full, clock, 1),
        ),
        (IncompleteReceipt("unknown", None, None),),
    )
    requested = (empty, full, missing)
    plan = plan_required_days(index, days=requested, require_nonempty=True)
    assert plan.unknown_incomplete_requests == ("unknown",)
    assert plan.days == (empty, missing)
    assert plan_required_days(index, days=requested, require_nonempty=False).days == (missing,)
