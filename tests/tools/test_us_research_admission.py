"""Real authoring CLI: current eligibility, no retrospective rewrite of saved reports."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256

import pytest
from tests.helpers.us_research_admission import (
    current_flags,
    make_current_us,
    write_calendar_capture,
)
from tests.tools.test_us_research_report_daily_close import DAY, _input
from tools.experiments.us_research_report import main

from baibai_engine.investment_judgment.us_proposal import (
    ReportDraft,
    ReportReview,
    ReviewedReport,
    draft_digest,
    render,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    encode_current_listing_master,
)

_SHORT = date.fromisoformat(DAY)
_NEXT = _SHORT + timedelta(days=3)
_CUTOFF = datetime.fromisoformat("2025-11-29T14:00:00Z")
_NOW = datetime.fromisoformat("2025-11-29T15:10:00Z")


def _write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _inputs(tmp_path, ticker="BRK.B"):
    draft, capture, _ = _input(tmp_path, ticker)
    path = _write(tmp_path / "source.json", draft)
    listing = tmp_path / "current.json"
    current = make_current_us(
        venue=draft["venue"], ticker=ticker, day=_SHORT, identity_since=_SHORT
    )
    listing.write_bytes(encode_current_listing_master({"US": current}))
    calendar = write_calendar_capture(
        tmp_path / "calendar.json",
        start=_SHORT,
        end=_NEXT,
        sessions={_SHORT: "13:00", _NEXT: "16:00"},
        received_at=_CUTOFF,
    )
    return draft, path, listing, current, calendar, ["--daily-close-proof", capture]


def test_shortened_friday_remains_eligible_at_confirmed_weekend_and_saved_read_skips_pins(
    tmp_path, capsys
):
    draft, source, listing, current, calendar, price = _inputs(tmp_path)
    original_digest = draft_digest(ReportDraft.model_validate(draft))
    flags = current_flags(listing, calendar)
    assert main(["draft-digest", source, *price, *flags], now=_NOW) == 0
    binding = json.loads(capsys.readouterr().out)
    assert binding["draft_sha256"] != original_digest
    review = _write(
        tmp_path / "review.json",
        {
            **binding,
            "reviewer_identity": "independent synthetic reviewer",
            "reviewed_at": _NOW.isoformat(),
            "conclusion": "pass",
            "notes": "Evidence fixture.",
        },
    )
    saved = tmp_path / "saved.json"
    args = ["save", source, review, "--output", str(saved), *price, *flags]
    assert main(args, now=_NOW) == 0
    value = ReviewedReport.model_validate_json(saved.read_bytes())
    assert value.draft.listing_basis is not None
    assert value.draft.listing_basis.listing_session_et == _SHORT
    assert value.draft.listing_basis.instrument_id == current.slice.rows[0].instrument_id
    assert draft_digest(value.draft) == binding["draft_sha256"]
    pin_paths = [listing, calendar, tmp_path / "bars.json"]
    for p in pin_paths:
        p.unlink()
    assert main(["render", str(saved)]) == 0
    rendered = capsys.readouterr().out
    assert sha256(rendered.encode()).hexdigest() == value.rendered_sha256
    assert "現行銘柄区分の対象立会" in rendered


@pytest.mark.parametrize(
    "fault",
    [
        "no_master",
        "no_calendar",
        "old_master",
        "other_market",
        "other_class",
        "sell_only",
        "buy_suspended",
        "fund",
        "changed_identity",
        "stale_listing_set",
        "late_scope",
        "bad_cal_digest",
        "late_calendar",
        "rounded_late_calendar",
        "wrong_calendar_url",
        "missing_calendar_end",
        "normal_close_not_short",
        "future_draft",
        "next_open",
        "newer_completed",
    ],
)
def test_new_report_never_uses_a_stale_or_unconfirmed_record(tmp_path, capsys, fault):
    draft, source, listing, current, calendar, price = _inputs(tmp_path)
    flags = current_flags(listing, calendar)
    now = _NOW
    old_date = _SHORT - timedelta(days=2)
    if fault in {"no_calendar", "no_master"}:
        flags = flags[2:] if fault == "no_master" else flags[:2]
    elif fault in {
        "old_master",
        "other_market",
        "other_class",
        "fund",
        "sell_only",
        "buy_suspended",
    }:
        current = make_current_us(
            venue="XNAS" if fault == "other_market" else "XNYS",
            ticker="BRK.A" if fault == "other_class" else "BRK.B",
            day=old_date if fault == "old_master" else _SHORT,
            identity_since=old_date,
            state=fault if fault in ("sell_only", "buy_suspended") else "catalog_target",
            issue_type="fund" if fault == "fund" else "not_flagged_fund",
        )
    elif fault in {"changed_identity", "stale_listing_set", "late_scope"}:
        assert current.rakuten is not None
        if fault == "changed_identity":
            current = replace(
                current,
                rakuten=replace(
                    current.rakuten,
                    rows=(replace(current.rakuten.rows[0], instrument_id="other-security"),),
                ),
            )
        elif fault == "late_scope":
            # Same NY session but later than the optimistic draft cutoff, which is moved
            # to Friday; the price proof and report dates remain otherwise usable.
            cutoff = datetime(2025, 11, 28, 21, tzinfo=UTC)
            draft.update(cutoff_at=cutoff.isoformat(), prepared_at=cutoff.isoformat())
            _write(tmp_path / "source.json", draft)
            current = replace(
                current,
                rakuten=replace(
                    current.rakuten, exclusions_received_at=cutoff + timedelta(minutes=1)
                ),
            )
            capture = json.loads(calendar.read_text())
            capture["received_at"] = cutoff.isoformat()
            _write(calendar, capture)
        else:
            current = replace(
                current,
                slice=replace(
                    current.slice,
                    rows=(
                        *current.slice.rows,
                        replace(
                            current.slice.rows[0], key=("XNYS", "OTHER"), instrument_id="new-target"
                        ),
                    ),
                ),
            )
    elif fault in {
        "late_calendar",
        "rounded_late_calendar",
        "wrong_calendar_url",
        "bad_cal_digest",
    }:
        capture = json.loads(calendar.read_text())
        if fault == "late_calendar":
            capture["received_at"] = (_CUTOFF + timedelta(seconds=1)).isoformat()
        elif fault == "rounded_late_calendar":
            capture["received_at"] = "2025-11-29T14:00:00.000000001Z"
        elif fault == "wrong_calendar_url":
            capture["response_url"] += "&ambiguous=1"
        else:
            capture["sha256"] = "9" * 64
        _write(calendar, capture)
    elif fault in {"missing_calendar_end", "normal_close_not_short"}:
        write_calendar_capture(
            calendar,
            start=_SHORT,
            end=_SHORT if fault == "missing_calendar_end" else _NEXT,
            sessions={_SHORT: "16:00" if fault == "normal_close_not_short" else "13:00"},
            received_at=_CUTOFF,
        )
    elif fault == "future_draft":
        now = _CUTOFF + timedelta(seconds=10)
    elif fault in {"next_open", "newer_completed"}:
        now = datetime(2025, 12, 1, 15 if fault == "next_open" else 22, tzinfo=UTC)
    listing.write_bytes(encode_current_listing_master({"US": current}))
    assert main(["draft-digest", source, *price, *flags], now=now) == 2
    result = capsys.readouterr()
    assert result.out == ""
    assert result.err == "FAIL draft\n"


def test_same_day_reused_id_invalidates_existing_review_digest_without_touching_saved(
    tmp_path, capsys
):
    _, source, listing, _, calendar, price = _inputs(tmp_path)
    flags = current_flags(listing, calendar)
    cmd = ["draft-digest", source, *price, *flags]
    assert main(cmd, now=_NOW) == 0
    before = json.loads(capsys.readouterr().out)
    review = _write(
        tmp_path / "review.json",
        {
            **before,
            "reviewer_identity": "other",
            "reviewed_at": _NOW.isoformat(),
            "conclusion": "pass",
            "notes": "Reviewed the old identity only.",
        },
    )
    current = make_current_us(
        venue="XNYS", ticker="BRK.B", day=_SHORT, identity_since=_SHORT, instrument_id="other-id"
    )
    listing.write_bytes(encode_current_listing_master({"US": current}))
    assert main(cmd, now=_NOW) == 0
    after = json.loads(capsys.readouterr().out)
    assert before["draft_sha256"] != after["draft_sha256"]
    saved = tmp_path / "must-not-exist.json"
    assert main(["save", source, review, "--output", str(saved), *price, *flags], now=_NOW) == 2
    assert not saved.exists()
    assert capsys.readouterr().err == "FAIL review/save\n"


def test_preclose_latest_completed_price_may_be_prior_session_but_not_for_a_new_identity(
    tmp_path, capsys
):
    draft, source, listing, _, calendar, price = _inputs(tmp_path)
    before_close = datetime(2025, 12, 1, 17, tzinfo=UTC)
    draft.update(cutoff_at=before_close.isoformat(), prepared_at=before_close.isoformat())
    _write(tmp_path / "source.json", draft)
    cmd = ["draft-digest", source, *price, *current_flags(listing, calendar)]
    for identity_since, expected in ((_SHORT, 0), (_NEXT, 2)):
        current = make_current_us(
            venue="XNYS", ticker="BRK.B", day=_NEXT, identity_since=identity_since
        )
        listing.write_bytes(encode_current_listing_master({"US": current}))
        assert main(cmd, now=before_close) == expected
        capsys.readouterr()


def test_preexisting_saved_report_canonical_json_and_render_remain_unbound(tmp_path, capsys):
    draft, _, _, _, _, _ = _inputs(tmp_path)
    flat = ReportDraft.model_validate(draft)
    review = ReportReview(
        report_id=flat.report_id,
        draft_sha256=draft_digest(flat),
        reviewer_identity="prior reviewer",
        reviewed_at=_NOW,
        conclusion="pass",
        notes="Legacy fixture, not a new admission.",
    )
    assert "listing_basis" not in flat.model_dump(mode="json")
    bytes_ = render(flat, review)
    stored = ReviewedReport(draft=flat, review=review, rendered_sha256=sha256(bytes_).hexdigest())
    path = _write(tmp_path / "old.json", stored.model_dump(mode="json"))
    assert main(["render", path]) == 0
    assert capsys.readouterr().out.encode() == bytes_
