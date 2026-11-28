"""Run the formal CLI using real local proof codecs and an isolated App."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Event

import pytest
from tests.helpers.us_research_admission import (
    current_flags,
    write_calendar_capture,
)
from tests.tools.test_us_research_admission import (
    _NEXT,
    _SHORT,
    _SHORT_CLOSE,
    _alpaca_listing,
    _inputs,
    _write,
)
from tools.experiments import us_research_report as cli

from baibai_engine.investment_judgment import us_proposal as owner
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    encode_current_listing_master,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.application.write import initialize_database

_REPORT_CUTOFF = datetime(2025, 11, 28, 20, 45, tzinfo=UTC)
_REPORT_PREPARED = datetime(2025, 11, 28, 21, tzinfo=UTC)
_DIGEST_AT = datetime(2025, 11, 28, 21, 5, tzinfo=UTC)
_REFRESH_AT = datetime(2025, 11, 28, 21, 6, tzinfo=UTC)
_SAVE_AT = datetime(2025, 11, 28, 21, 10, tzinfo=UTC)
_LATE = datetime(2025, 12, 1, 22, tzinfo=UTC)


def _refresh(listing, calendar, *, ticker="BRK.B", number=1, at=_REFRESH_AT):
    value = _alpaca_listing(
        ticker=ticker, number=number, observed=_SHORT_CLOSE - timedelta(minutes=1), received=at
    )
    listing.write_bytes(encode_current_listing_master({"US": value}))
    write_calendar_capture(
        calendar,
        start=_SHORT,
        end=_NEXT,
        sessions={_SHORT: "13:00", _NEXT: "16:00"},
        received_at=at,
    )


def _prepare(tmp_path, capsys, *, action="no_action", unbound=False):
    raw, draft, listing, _, calendar, price = _inputs(tmp_path)
    raw.update(cutoff_at=_REPORT_CUTOFF.isoformat(), prepared_at=_REPORT_PREPARED.isoformat())
    if action == "defer":
        raw.update(
            official_close=None,
            daily_close=None,
            comparison=None,
            unresolved=["Synthetic close not yet verified."],
        )
        price = []
    else:
        raw["comparison"]["horizon_et"] = "2026-11-27"
    _write(tmp_path / "source.json", raw)
    _refresh(listing, calendar, at=_REPORT_CUTOFF)
    flags = [*price, *current_flags(listing, calendar)]
    reviewed = tmp_path / "analysis.json"
    if not unbound:
        assert cli.main(["draft-digest", draft, *flags], now=_REPORT_PREPARED) == 0
        report_binding = json.loads(capsys.readouterr().out)
    else:
        legacy = owner.ReportDraft.model_validate(raw)
        report_binding = {"report_id": legacy.report_id, "draft_sha256": owner.draft_digest(legacy)}
    report_review = {
        **report_binding,
        "reviewer_identity": "synthetic independent analysis peer",
        "reviewed_at": _REPORT_PREPARED.isoformat(),
        "conclusion": "pass",
        "notes": "Synthetic analysis review.",
    }
    if not unbound:
        proof = _write(tmp_path / "analysis-review.json", report_review)
        assert (
            cli.main(
                ["save", draft, proof, "--output", str(reviewed), *flags],
                now=_REPORT_PREPARED,
            )
            == 0
        )
    else:
        stored = owner.reviewed_report(legacy, owner.ReportReview.model_validate(report_review))
        reviewed.write_bytes(stored.model_dump_json().encode())
    decision_raw = {
        "proposal_id": "us-proposal-current-admission",
        "author_identity": "synthetic formal author",
        "prepared_at": _DIGEST_AT.isoformat(),
        "action": action,
        "reason": "Synthetic decision justified by its separate analysis.",
        "revisit_when": "Upon new relevant issuer information.",
    }
    decision = _write(tmp_path / "decision.json", decision_raw)
    digest = ["proposal-digest", str(reviewed), decision, *flags]
    if not unbound:
        assert cli.main(digest, now=_DIGEST_AT) == 0
        expected = json.loads(capsys.readouterr().out)
    else:
        # Produce the historically valid digest without reopening new admission.
        payload = {
            "decision": owner.USProposalDraft.model_validate(decision_raw).model_dump(mode="json"),
            "analysis": stored.model_dump(mode="json"),
        }
        expected = {
            "proposal_id": decision_raw["proposal_id"],
            "draft_sha256": sha256(canonical_json(payload).encode()).hexdigest(),
        }
    decision_review_data = {
        **expected,
        "reviewer_identity": "synthetic independent formal peer",
        "reviewed_at": _DIGEST_AT.isoformat(),
        "conclusion": "pass",
        "notes": "Synthetic separate formal review.",
    }
    review = _write(tmp_path / "proposal-review.json", decision_review_data)
    app = tmp_path / "app #isolated.sqlite"
    initialize_database(app)
    publish = ["proposal-save", str(reviewed), decision, review, "--db", str(app), *flags]
    if unbound:
        legacy_id = "us-proposal-previously-saved"
        legacy_decision = {**payload["decision"], "proposal_id": legacy_id}
        old_digest = sha256(
            canonical_json({**payload, "decision": legacy_decision}).encode()
        ).hexdigest()
        record = owner.USProposal(
            decision=legacy_decision,
            analysis=stored,
            review={**decision_review_data, "proposal_id": legacy_id, "draft_sha256": old_digest},
            published_at=_SAVE_AT,
        )
        with closing(sqlite3.connect(app, autocommit=True)) as connection:
            connection.execute(
                "INSERT INTO us_equity_proposal VALUES (?,?,?)",
                (
                    legacy_id,
                    _SAVE_AT.isoformat(timespec="microseconds"),
                    canonical_json(record.model_dump(mode="json")),
                ),
            )
    return digest, publish, app, listing, calendar, expected


@pytest.mark.parametrize("action", ["no_action", "defer"])
def test_refreshed_same_session_identity_preserves_the_review_and_saved_read(
    tmp_path, capsys, action
):
    digest, publish, app, listing, calendar, expected = _prepare(tmp_path, capsys, action=action)
    _refresh(listing, calendar)
    # Latest receipts intentionally postdate the signed analysis's cutoff.
    assert cli.main(digest, now=_SAVE_AT) == 0
    assert json.loads(capsys.readouterr().out) == expected
    assert cli.main(publish, now=_SAVE_AT) == 0
    assert json.loads(capsys.readouterr().out) == expected
    before = app.read_bytes()
    for path in (listing, calendar, tmp_path / "bars.json"):
        path.unlink()
    for select in ([], ["--proposal-id", expected["proposal_id"]]):
        assert (
            cli.main(
                ["proposal-show", "--db", str(app), *select],
                now=datetime(2027, 12, 8, tzinfo=UTC),
            )
            == 0
        )
        saved = json.loads(capsys.readouterr().out)
        assert saved["decision"]["action"] == action
        assert saved["analysis"]["draft"]["listing_basis"]["listing_session_et"] == str(_SHORT)
        assert saved["review"]["draft_sha256"] == expected["draft_sha256"]
        assert saved["published_at"] == _SAVE_AT.isoformat().replace("+00:00", "Z")
    assert app.read_bytes() == before
    with closing(sqlite3.connect(app)) as connection:
        for table in ("investment_proposal", "operation_session", "ledger_event"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


@pytest.mark.parametrize(
    "fault",
    [
        "listing_removed",
        "remap",
        "next_before_close",
        "next_completed",
        "missing_pin",
        "missing_calendar",
        "no_flags",
        "future_receipt",
        "ineligible_defer",
        "unbound_analysis",
    ],
)
def test_review_from_an_earlier_input_cannot_authorize_new_decision(tmp_path, capsys, fault):
    legacy = fault == "unbound_analysis"
    digest, publish, app, listing, calendar, expected = _prepare(
        tmp_path,
        capsys,
        action="defer" if legacy or fault == "ineligible_defer" else "no_action",
        unbound=legacy,
    )
    at = _SAVE_AT
    if fault in {"listing_removed", "ineligible_defer"}:
        _refresh(listing, calendar, ticker="BRK.A")
    elif fault == "remap":
        # An eligible current quote for the same label now identifies a different UUID.
        _refresh(listing, calendar, number=2)
    elif fault == "next_before_close":
        at = datetime(2025, 12, 1, 17, tzinfo=UTC)
        # Same instrument freshly confirmed on Monday; old Friday basis still cannot be used
        # to authorize a *new* decision even though Friday's close is still the latest close.
        listing.write_bytes(
            encode_current_listing_master({"US": _alpaca_listing(_NEXT, observed=_SHORT_CLOSE)})
        )
    elif fault == "next_completed":
        at = _LATE
    elif fault == "missing_pin":
        listing.unlink()
    elif fault == "missing_calendar":
        calendar.unlink()
    elif fault == "no_flags":
        digest, publish = digest[:-4], publish[:-4]
    elif fault == "future_receipt":
        _refresh(listing, calendar, at=datetime(2025, 11, 28, 21, 11, tzinfo=UTC))
    before = app.read_bytes()
    for command, phase in ((digest, "draft"), (publish, "save")):
        assert cli.main(command, now=at) == 2
        assert capsys.readouterr().err == f"FAIL US proposal {phase}\n"
    assert app.read_bytes() == before
    with closing(sqlite3.connect(app)) as connection:
        assert connection.execute("SELECT count(*) FROM us_equity_proposal").fetchone() == (
            int(legacy),
        )
    if legacy:
        assert cli.main(["proposal-show", "--db", str(app)], now=_LATE) == 0
        stored = json.loads(capsys.readouterr().out)
        assert stored["decision"]["proposal_id"] == "us-proposal-previously-saved"
        assert "listing_basis" not in stored["analysis"]["draft"]
        key = ["--proposal-id", expected["proposal_id"]]
        assert cli.main(["proposal-show", "--db", str(app), *key]) == 1
        assert capsys.readouterr().out == "null\n"


@pytest.mark.parametrize("during_wait", ["clock", "pin"])
def test_save_checks_after_a_real_app_write_lock_wait(tmp_path, capsys, monkeypatch, during_wait):
    _, publish, app, listing, calendar, _ = _prepare(tmp_path, capsys)
    original_connect, original_read = owner.connect_existing, cli._current_inputs
    waiting = Event()
    active_clock = [_SAVE_AT]
    pin_reads = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return active_clock[0].astimezone(tz)

    def trace_connect(path):
        connection = original_connect(path)
        connection.set_trace_callback(
            lambda sql: waiting.set() if sql == "BEGIN IMMEDIATE" else None
        )
        return connection

    def trace_pin(args):
        pin_reads.append(active_clock[0])
        return original_read(args)

    monkeypatch.setattr(owner, "connect_existing", trace_connect)
    monkeypatch.setattr(cli, "_current_inputs", trace_pin)
    monkeypatch.setattr(cli, "datetime", Clock)
    before = app.read_bytes()
    with (
        closing(sqlite3.connect(app, autocommit=True)) as blocker,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        blocker.execute("BEGIN IMMEDIATE")
        pending = pool.submit(cli.main, publish)
        try:
            assert waiting.wait(timeout=5)
            assert pin_reads == []
            if during_wait == "clock":
                active_clock[0] = _LATE  # Actual NY close moved while waiting.
            else:
                _refresh(listing, calendar, ticker="BRK.A")
        finally:
            blocker.execute("ROLLBACK")
        assert pending.result(timeout=5) == 2
    assert pin_reads == active_clock
    assert capsys.readouterr().err == "FAIL US proposal save\n"
    assert app.read_bytes() == before
    assert cli.main(["proposal-show", "--db", str(app)]) == 1
    assert capsys.readouterr().out == "null\n"
