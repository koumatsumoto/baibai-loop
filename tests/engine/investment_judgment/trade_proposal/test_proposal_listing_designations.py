"""提案準備は保存された当該時点のJPX指定だけを伝え、査定や売買権利に昇格させない。"""

from contextlib import closing
from datetime import UTC, timedelta

import pytest
from tests.helpers.proposal import PREPARED, PROPOSAL_ID, backend_case

from baibai_engine.data_collection.providers.jpx.models import JPXResponse
from baibai_engine.data_collection.providers.jpx.receipts import (
    complete_bundle,
    create_bundle,
    retain_bundle_response,
)
from baibai_engine.investment_judgment.trade_proposal.inputs import build_proposal_context
from baibai_engine.investment_judgment.trade_proposal.scaffold import scaffold_proposal
from baibai_engine.machine_analysis.observations.disclosures.jpx_sources import JPX_REGULATION_URLS
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def _page(ticker):
    return (
        "<h2>現在の指定状況</h2><h3>監理銘柄（確認中）</h3><table class='fixedhead'>"
        "<tr><th>指定年月日</th><th>銘柄名</th><th>コード</th></tr>"
        f"<tr><td>2026/10/13</td><td>例</td><td>{ticker}</td></tr></table>"
        "<h3>監理銘柄（審査中）</h3><p>現在、該当する情報はありません。</p>"
        "<h3>整理銘柄</h3><p>現在、該当する情報はありません。</p>"
    ).encode()


def _bundle(case, *, start, page, finish=True):
    start = start.astimezone(UTC)
    with closing(open_connection(case.market)) as conn:
        identity = create_bundle(
            conn, "regulations", start.date(), start, tuple(JPX_REGULATION_URLS.items())
        )
        for i, (name, url) in enumerate(JPX_REGULATION_URLS.items()):
            # This observation refers to one page, not the completion time of the last member.
            retain_bundle_response(
                conn,
                identity,
                name,
                JPXResponse(
                    url,
                    start + timedelta(seconds=i),
                    "text/html; charset=utf-8",
                    page if name == "整理銘柄" else b"<html></html>",
                ),
            )
        if finish:
            complete_bundle(conn, identity)
    return identity


def _context(case, at=PREPARED):
    return build_proposal_context(
        operation_id=case.operation.operation_id,
        proposal_id=PROPOSAL_ID,
        db_path=case.app,
        sqlite_path=case.market,
        now=at,
    )


@pytest.mark.parametrize("holding", [False, True])
def test_targeted_observation_and_draft_use_receipt_not_future_or_another_code(tmp_path, holding):
    case = backend_case(tmp_path, holding=holding)
    no_source = _context(case)
    assert no_source.listing_designations.status == "no_eligible_bundle"
    start = PREPARED - timedelta(minutes=3)
    old = _bundle(case, start=start, page=_page("1234"))
    # The future complete page lists a different company; it cannot erase today's observation.
    future = _bundle(case, start=PREPARED + timedelta(minutes=1), page=_page("5678"))
    original = (case.app.read_bytes(), case.market.read_bytes())
    ctx = _context(case)
    obs = ctx.listing_designations
    assert obs.status == "observed"
    assert obs.bundle_id == old
    assert obs.page_received_at == start + timedelta(seconds=1)
    assert obs.completed_at == start + timedelta(seconds=3)
    assert obs.designations_by_ticker == {"1234": ("監理銘柄（確認中）",)}
    assert ctx.inputs == no_source.inputs
    assert ctx.calculations == no_source.calculations
    draft = scaffold_proposal(
        operation_id=case.operation.operation_id,
        proposal_id=PROPOSAL_ID,
        db_path=case.app,
        sqlite_path=case.market,
        now=PREPARED,
    )
    assert draft["items"][0]["action"] == "defer"
    reasons = draft["items"][0]["review_conditions"]
    assert sum("監理銘柄（確認中）" in r for r in reasons) == 1
    assert "listing_designations" not in draft
    later = _context(case, PREPARED + timedelta(minutes=2)).listing_designations
    assert later.bundle_id == future
    assert later.designations_by_ticker == {"1234": ()}
    assert original == (case.app.read_bytes(), case.market.read_bytes())


def test_incomplete_or_unreadable_receipt_never_turns_old_or_missing_row_into_clearance(tmp_path):
    case = backend_case(tmp_path)
    _bundle(case, start=PREPARED - timedelta(minutes=4), page=_page("1234"), finish=False)
    with closing(open_connection(case.market)) as conn, conn:
        # Legacy-derived rows cannot evidence what the original supervision section said.
        conn.execute(
            "INSERT INTO jpx_regulation_flags VALUES(?, '整理銘柄','1234','整理銘柄',?)",
            (PREPARED.date().isoformat(), PREPARED.isoformat()),
        )
    old = _context(case).listing_designations
    assert old.status == "no_eligible_bundle"
    assert old.designations_by_ticker is None
    _bundle(case, start=PREPARED - timedelta(minutes=3), page=_page("1234"))
    bad = _bundle(case, start=PREPARED - timedelta(minutes=2), page=b"<html>old layout</html>")
    seen = _context(case).listing_designations
    assert seen.status == "unreadable_bundle"
    assert seen.bundle_id == bad
    assert seen.designations_by_ticker is None


def test_equal_completed_snapshots_are_not_ordered_by_uuid(tmp_path):
    case = backend_case(tmp_path)
    stamp = PREPARED - timedelta(minutes=3)
    for code in ("1234", "5678"):
        _bundle(case, start=stamp, page=_page(code))
    check = _context(case).listing_designations
    assert check.status == "ambiguous_bundle"
    assert check.designations_by_ticker is None
