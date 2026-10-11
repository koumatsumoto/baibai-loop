from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from baibai_batch.jobs.retail_cohorts import (
    PairEntry,
    journal_bytes,
    load_journal,
    load_plan,
    make_plan,
    mark_inflight,
    new_journal,
    plan_bytes,
    record_outcome,
    summarize,
    validate_resume,
)
from baibai_engine.batch_api import (
    RetailAnalystRatingObservation,
    RetailCollectionOutcome,
    RetailModelValuationObservation,
)


def plan(**kwargs):
    return make_plan(
        **{
            "run_id": "2026-10-11",
            "tickers": ("130A", "7203"),
            "universe_binding": "synthetic-master-sha256",
            "collector_binding": "synthetic-code-sha256",
            **kwargs,
        }
    )


def observation(source="monex", ticker="130A", *, missing=False, **kwargs):
    model = RetailModelValuationObservation if source == "monex" else RetailAnalystRatingObservation
    return model.model_validate(
        {
            "ticker": ticker,
            "fetched_at_utc": "2026-10-11T00:00:01.123456+00:00",
            "distributor": source,
            "source_url": f"https://synthetic.invalid/{source}",
            "parser_version": "synthetic-1",
            **(
                {"pbr": None if missing else 0}
                if source == "monex"
                else {"rating": None if missing else 0}
            ),
            **kwargs,
        }
    )


def finish(p, journal, source, ticker, *, missing=False):
    active = mark_inflight(p, journal, source, ticker)
    return record_outcome(
        p,
        active,
        RetailCollectionOutcome(source, ticker, "observed", http_status=200),
        observation(source, ticker, missing=missing),
    )


def test_plan_has_deterministic_disjoint_bounded_cohorts():
    tickers = tuple(str(1000 + index) for index in range(301))
    p = plan(tickers=tuple(reversed(tickers)), cohort_size=150)
    same = plan(tickers=tickers, cohort_size=150)
    assert p == same
    assert p.plan_sha256 == same.plan_sha256
    assert [len(c.tickers) for c in p.cohorts] == [150, 150, 1]
    pairs = [pair for cohort in p.cohorts for pair in cohort.pairs]
    assert len(pairs) == len(set(pairs)) == 602
    assert set(pairs) == {(source, ticker) for source in p.sources for ticker in tickers}
    assert [len(c.tickers) for c in plan(tickers=tickers[:51]).cohorts] == [50, 1]
    with pytest.raises(FrozenInstanceError):
        p.run_id = "changed"


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": "2026-10-12"},
        {"universe_binding": "other-master"},
        {"collector_binding": "other-code"},
        {"sources": ("monex",)},
        {"cohort_size": 1},
    ],
)
def test_resume_rejects_changed_plan_binding(change):
    original = plan()
    journal = new_journal(original, 0)
    changed = plan(**change)
    assert original.plan_sha256 != changed.plan_sha256
    with pytest.raises(ValueError, match="binding"):
        load_journal(changed, journal_bytes(original, journal))


@pytest.mark.parametrize(
    "change",
    [
        {"tickers": ("7203", "7203")},
        {"tickers": ()},
        {"tickers": ("130a",)},
        {"tickers": (" 7203",)},
        {"tickers": ("ABCD",)},
        {"tickers": ("７２０３",)},
        {"cohort_size": 151},
        {"cohort_size": 0},
        {"cohort_size": True},
        {"sources": ()},
        {"sources": ("monex", "monex")},
        {"sources": ("unknown",)},
        {"universe_binding": ""},
    ],
)
def test_invalid_plan_cannot_be_persisted(change):
    with pytest.raises(ValueError, match=r"duplicate universe|plan requires|cohort size"):
        plan(**change)


def test_private_checkpoint_roundtrip_keeps_receipt_and_zero_partial_null():
    p = plan()
    journal = finish(p, new_journal(p, 0), "monex", "130A")
    payload = journal_bytes(p, journal)
    restored = load_journal(load_plan(plan_bytes(p)), payload)
    assert restored == journal
    assert journal_bytes(p, restored) == payload
    assert restored.entries[0].state == "success"
    assert restored.observations[0].pbr == 0
    assert restored.observations[0].consensus_target is None
    assert restored.observations[0].fetched_at_utc.isoformat() == "2026-10-11T00:00:01.123456+00:00"
    assert b"synthetic.invalid" in payload
    assert b"raw_html" not in payload


def test_normal_null_is_an_observation_and_is_separate_from_failure():
    p = plan(tickers=("130A",))
    journal = finish(p, new_journal(p, 0), "monex", "130A", missing=True)
    journal = mark_inflight(p, journal, "rakuten", "130A")
    journal = record_outcome(
        p,
        journal,
        RetailCollectionOutcome(
            "rakuten", "130A", "parse_failure", "no_estimates_identity_unconfirmed", 200
        ),
    )
    report = summarize(p, [journal], require_all=True)
    assert report["state_counts"] == {
        "failure": 1,
        "normal_null": 1,
        "success": 0,
        "unattempted": 0,
        "uncertain": 0,
    }
    assert len(journal.observations) == 1
    assert report["whole_universe_accounting"] is True
    assert report["attempted_completeness"] is True
    assert report["success_completeness"] is False
    assert journal.halt_reason is None
    validate_resume(p, [journal])


def test_accounting_does_not_claim_attempted_or_success_complete():
    p = plan(cohort_size=1)
    journals = [new_journal(p, c.index) for c in p.cohorts]
    report = summarize(p, journals)
    assert report["whole_universe_accounting"] is True
    assert report["attempted_completeness"] is False
    assert report["success_completeness"] is False
    for index, journal in enumerate(journals):
        for source, ticker in p.cohorts[index].pairs:
            journal = finish(p, journal, source, ticker, missing=source == "rakuten")
        journals[index] = journal
    report = summarize(p, journals, require_all=True)
    assert report["success_completeness"] is True
    assert report["state_counts"]["normal_null"] == 2


def test_missing_duplicate_and_changed_cohorts_are_rejected():
    p = plan(cohort_size=1)
    journal = new_journal(p, 0)
    report = summarize(p, [journal])
    assert report["missing_cohorts"] == 1
    assert report["unrecorded_pairs"] == 2
    assert report["state_counts"]["unattempted"] == 4
    assert report["whole_universe_accounting"] is False
    with pytest.raises(ValueError, match="missing cohort"):
        summarize(p, [journal], require_all=True)
    with pytest.raises(ValueError, match="duplicate cohort"):
        validate_resume(p, [journal, journal])
    with pytest.raises(ValueError, match="unknown cohort"):
        journal_bytes(p, replace(journal, cohort_index=2))


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_pair",
        "duplicate_pair",
        "reordered_pairs",
        "raw_html",
        "wrong_state",
        "unfixed_reason",
        "wrong_ticker",
        "invalid_pair_ticker",
    ],
)
def test_private_journal_rejects_corrupt_shape_or_semantics(mutation):
    p = plan()
    journal = finish(p, new_journal(p, 0), "monex", "130A")
    data = json.loads(journal_bytes(p, journal))
    entries = data["entries"]
    if mutation == "missing_pair":
        entries.pop()
    elif mutation == "duplicate_pair":
        entries[-1] = entries[0]
    elif mutation == "reordered_pairs":
        entries.reverse()
    elif mutation == "raw_html":
        entries[0]["raw_html"] = "licensed material must not enter this contract"
    elif mutation == "wrong_state":
        entries[0]["state"] = "normal_null"
        entries[0]["reason"] = "all_values_missing"
    elif mutation == "unfixed_reason":
        entries[1]["reason"] = "private exception or response body"
    elif mutation == "invalid_pair_ticker":
        entries[1]["ticker"] = "７２０３"
    else:
        entries[0]["observation"]["ticker"] = "7203"
    with pytest.raises(
        ValueError,
        match=r"journal pairs|unexpected checkpoint|pair state|fixed outcome reason|observation does not match|canonical pair identity",
    ):
        load_journal(p, json.dumps(data).encode())


@pytest.mark.parametrize("ticker", ["ABCD", "７２０３", "130a", "7203.T", " 7203"])
def test_pair_constructor_rejects_noncanonical_tickers_before_any_acquisition(ticker):
    with pytest.raises(ValueError, match="canonical pair identity"):
        PairEntry("monex", ticker, "unattempted", "not_started")


@pytest.mark.parametrize("mutation", ["missing_ticker", "boolean_index", "duplicate_cohort"])
def test_saved_plan_rejects_changed_cohort_and_duplicate_json_fields(mutation):
    p = plan()
    data = json.loads(plan_bytes(p))
    if mutation == "missing_ticker":
        data["cohorts"][0]["tickers"].pop()
    elif mutation == "boolean_index":
        data["cohorts"][0]["index"] = False
    else:
        data["cohorts"].append(data["cohorts"][0])
    with pytest.raises(ValueError, match="cohorts differ"):
        load_plan(json.dumps(data).encode())
    with pytest.raises(ValueError, match="duplicate JSON"):
        load_plan(b'{"format":"retail-daily-plan-v1","format":"other"}')


def test_terminal_replay_is_idempotent_but_changed_value_or_receipt_conflicts():
    p = plan()
    journal = finish(p, new_journal(p, 0), "monex", "130A")
    outcome = RetailCollectionOutcome("monex", "130A", "observed", http_status=200)
    assert record_outcome(p, journal, outcome, observation()) is journal
    for changed in (observation(pbr=1), observation(fetched_at_utc="2026-10-11T00:00:02+00:00")):
        with pytest.raises(ValueError, match="payload cannot change"):
            record_outcome(p, journal, outcome, changed)
    with pytest.raises(ValueError, match="already attempted"):
        mark_inflight(p, journal, "monex", "130A")


def test_durable_inflight_halts_resume_without_relabeling_as_unattempted():
    p = plan()
    initial = new_journal(p, 0)
    active = mark_inflight(p, initial, "monex", "130A")
    restored = load_journal(p, journal_bytes(p, active))
    assert initial.entries[0].state == "unattempted"
    assert restored.entries[0].state == "uncertain"
    assert restored.halt_reason == "inflight_uncertain"
    with pytest.raises(ValueError, match="daily run is halted"):
        validate_resume(p, [restored])
    with pytest.raises(ValueError, match="after a halt"):
        mark_inflight(p, restored, "rakuten", "130A")
    with pytest.raises(ValueError, match="cannot become unattempted"):
        record_outcome(
            p,
            restored,
            RetailCollectionOutcome("monex", "130A", "not_attempted", "wall_clock_limit"),
        )
    completed = record_outcome(
        p,
        active,
        RetailCollectionOutcome("monex", "130A", "observed", http_status=200),
        observation(),
    )
    validate_resume(p, [completed])


@pytest.mark.parametrize(("status", "reason"), [(403, "access_denied"), (429, "rate_limited")])
def test_blocked_access_halts_whole_daily_run_across_cohorts(status, reason):
    p = plan(cohort_size=1)
    active = mark_inflight(p, new_journal(p, 0), "monex", "130A")
    halted = record_outcome(
        p, active, RetailCollectionOutcome("monex", "130A", reason, http_status=status)
    )
    pending = new_journal(p, 1)
    assert halted.halt_reason == reason
    with pytest.raises(ValueError, match="daily run is halted"):
        validate_resume(p, [pending, halted])
    assert summarize(p, [halted, pending])["halt_reasons"] == {reason: 1}


def test_safe_public_summary_contains_counts_and_fixed_reasons_only():
    p = plan(tickers=("130A",))
    journal = finish(p, new_journal(p, 0), "monex", "130A")
    journal = finish(p, journal, "rakuten", "130A", missing=True)
    rendered = json.dumps(summarize(p, [journal]))
    for private in (
        "130A",
        "2026-10-11",
        "synthetic.invalid",
        "pbr",
        "rating",
        "parser_version",
        "fetched_at_utc",
        "synthetic-code",
    ):
        assert private not in rendered
    assert "all_values_missing" in rendered


def test_journal_and_pair_are_immutable_and_unattempted_cannot_claim_a_response():
    p = plan()
    journal = new_journal(p, 0)
    with pytest.raises(FrozenInstanceError):
        journal.entries = ()
    with pytest.raises(FrozenInstanceError):
        journal.entries[0].state = "failure"
    with pytest.raises(ValueError, match="cannot have an HTTP response"):
        PairEntry("monex", "130A", "unattempted", "not_started", 200)


def test_outcome_requires_marker_matching_typed_observation_and_safe_reason():
    p = plan()
    initial = new_journal(p, 0)
    outcome = RetailCollectionOutcome("monex", "130A", "observed", http_status=200)
    with pytest.raises(ValueError, match="saved inflight"):
        record_outcome(p, initial, outcome, observation())
    active = mark_inflight(p, initial, "monex", "130A")
    with pytest.raises(ValueError, match="does not match its pair"):
        record_outcome(p, active, outcome, observation("rakuten"))
    with pytest.raises(ValueError, match="fixed outcome reason"):
        record_outcome(
            p,
            active,
            RetailCollectionOutcome("monex", "130A", "parse_failure", "private raw exception", 200),
        )
    with pytest.raises(ValueError, match="outside the fixed cohort"):
        record_outcome(
            p,
            active,
            RetailCollectionOutcome("monex", "9432", "observed", http_status=200),
            observation(ticker="9432"),
        )


def test_incomplete_received_request_remains_uncertain_and_unattempted_stays_distinct():
    p = plan()
    active = mark_inflight(p, new_journal(p, 0), "monex", "130A")
    journal = record_outcome(
        p, active, RetailCollectionOutcome("monex", "130A", "incomplete", "wall_clock_limit")
    )
    journal = record_outcome(
        p, journal, RetailCollectionOutcome("rakuten", "130A", "not_attempted", "wall_clock_limit")
    )
    assert journal.entries[0].state == "uncertain"
    assert journal.entries[2].state == "unattempted"
    assert summarize(p, [journal])["state_counts"]["uncertain"] == 1
    with pytest.raises(ValueError, match="daily run is halted"):
        validate_resume(p, [journal])
