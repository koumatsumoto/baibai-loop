from __future__ import annotations

import json
import uuid
from dataclasses import FrozenInstanceError, replace

import pytest

from baibai_engine.machine_analysis.observations.external_assessments.models import AnalystRatingObservation as ExternalAssessmentAnalystRatingObservation
from baibai_jobs.acquisition.external_assessments.collection import CollectionOutcome as ExternalAssessmentCollectionOutcome
from baibai_engine.machine_analysis.observations.external_assessments.models import ModelValuationObservation as ExternalAssessmentModelValuationObservation
from baibai_jobs.jobs.external_assessment_cohorts import (
    PairEntry,
    load_plan,
    load_result,
    make_plan,
    new_result,
    plan_bytes,
    record_outcome,
    result_value,
    summarize,
)


def plan(**kwargs):
    return make_plan(
        **{
            "run_id": "2026-10-11",
            "tickers": ("130A", "7203"),
            "universe_binding": "synthetic-master-sha256",
            "collector_binding": "synthetic-code-sha256",
            "sources": ("monex", "rakuten"),
            **kwargs,
        }
    )


def observation(source="monex", ticker="130A", *, missing=False, **kwargs):
    model = (
        ExternalAssessmentModelValuationObservation
        if source == "monex"
        else ExternalAssessmentAnalystRatingObservation
    )
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


def attempt_for(source, ticker):
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"synthetic/{source}/{ticker}"))


def finish(p, journal, source, ticker, *, missing=False):
    active = journal
    return record_outcome(
        p,
        active,
        ExternalAssessmentCollectionOutcome(
            source,
            ticker,
            "observed",
            http_status=200,
            attempt_id=attempt_for(source, ticker),
        ),
        observation(source, ticker, missing=missing),
    )


def test_plan_has_deterministic_disjoint_bounded_cohorts():
    tickers = tuple(str(1000 + index) for index in range(301))
    p = plan(tickers=tuple(reversed(tickers)), cohort_size=50)
    same = plan(tickers=tickers, cohort_size=50)
    assert p == same
    assert p.plan_sha256 == same.plan_sha256
    assert [len(c.tickers) for c in p.cohorts] == [50, 50, 50, 50, 50, 50, 1]
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
    journal = new_result(original, 0)
    changed = replace(original, **change)
    assert original.plan_sha256 != changed.plan_sha256
    assert plan_bytes(load_plan(plan_bytes(original))) == plan_bytes(original)
    assert plan_bytes(load_plan(plan_bytes(changed))) == plan_bytes(changed)
    with pytest.raises(ValueError, match="binding"):
        load_result(changed, result_value(original, journal))


@pytest.mark.parametrize(
    "change",
    [
        {"tickers": ("7203", "7203")},
        {"tickers": ()},
        {"tickers": ("130a",)},
        {"tickers": (" 7203",)},
        {"tickers": ("ABCD",)},
        {"tickers": ("７２０３",)},
        {"cohort_size": 51},
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
    journal = finish(p, new_result(p, 0), "monex", "130A")
    payload = result_value(p, journal)
    restored = load_result(load_plan(plan_bytes(p)), payload)
    assert restored == journal
    assert result_value(p, restored) == payload
    assert restored.entries[0].state == "success"
    assert restored.observations[0].pbr == 0
    assert restored.observations[0].consensus_target is None
    assert restored.observations[0].fetched_at_utc.isoformat() == "2026-10-11T00:00:01.123456+00:00"
    assert "synthetic.invalid" in json.dumps(payload)
    assert "raw_html" not in json.dumps(payload)


def test_normal_null_is_an_observation_and_is_separate_from_failure():
    p = plan(tickers=("130A",))
    journal = finish(p, new_result(p, 0), "monex", "130A", missing=True)
    journal = record_outcome(
        p,
        journal,
        ExternalAssessmentCollectionOutcome(
            "rakuten",
            "130A",
            "parse_failure",
            "no_estimates_identity_unconfirmed",
            200,
            attempt_for("rakuten", "130A"),
        ),
    )
    report = summarize(p, [journal], require_all=True)
    assert report["state_counts"] == {
        "failure": 1,
        "normal_null": 1,
        "success": 0,
        "unattempted": 0,
    }
    assert len(journal.observations) == 1
    assert report["whole_universe_accounting"] is True
    assert report["attempted_completeness"] is True
    assert report["success_completeness"] is False
    assert journal.complete


def test_accounting_does_not_claim_attempted_or_success_complete():
    p = plan(cohort_size=1)
    journals = [new_result(p, c.index) for c in p.cohorts]
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
    journal = new_result(p, 0)
    report = summarize(p, [journal])
    assert report["missing_cohorts"] == 1
    assert report["unrecorded_pairs"] == 2
    assert report["state_counts"]["unattempted"] == 4
    assert report["whole_universe_accounting"] is False
    with pytest.raises(ValueError, match="missing cohort"):
        summarize(p, [journal], require_all=True)
    with pytest.raises(ValueError, match="duplicate cohort"):
        summarize(p, [journal, journal])
    with pytest.raises(ValueError, match="unknown cohort"):
        result_value(p, replace(journal, cohort_index=2))


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
    journal = finish(p, new_result(p, 0), "monex", "130A")
    data = result_value(p, journal)
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
        match=r"result pairs|unexpected fields|pair state|fixed outcome reason|observation does not match|canonical pair identity",
    ):
        load_result(p, data)


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
        load_plan(b'{"format":"external-assessment-daily-plan-v2","format":"other"}')


def test_old_typed_only_plan_format_cannot_resume_as_raw_bound_plan():
    data = json.loads(plan_bytes(plan()))
    data["format"] = "external-assessment-daily-plan-v1"
    with pytest.raises(ValueError, match="invalid daily plan format"):
        load_plan(json.dumps(data).encode())


def test_terminal_replay_is_idempotent_but_changed_value_or_receipt_conflicts():
    p = plan()
    journal = finish(p, new_result(p, 0), "monex", "130A")
    outcome = ExternalAssessmentCollectionOutcome(
        "monex",
        "130A",
        "observed",
        http_status=200,
        attempt_id=attempt_for("monex", "130A"),
    )
    assert record_outcome(p, journal, outcome, observation()) is journal
    for changed in (observation(pbr=1), observation(fetched_at_utc="2026-10-11T00:00:02+00:00")):
        with pytest.raises(ValueError, match="payload cannot change"):
            record_outcome(p, journal, outcome, changed)


def test_safe_public_summary_contains_counts_and_fixed_reasons_only():
    p = plan(tickers=("130A",))
    journal = finish(p, new_result(p, 0), "monex", "130A")
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
    journal = new_result(p, 0)
    with pytest.raises(FrozenInstanceError):
        journal.entries = ()
    with pytest.raises(FrozenInstanceError):
        journal.entries[0].state = "failure"
    with pytest.raises(ValueError, match="cannot have an HTTP response"):
        PairEntry("monex", "130A", "unattempted", "not_started", 200)


def test_outcome_requires_marker_matching_typed_observation_and_safe_reason():
    p = plan()
    initial = new_result(p, 0)
    outcome = ExternalAssessmentCollectionOutcome(
        "monex",
        "130A",
        "observed",
        http_status=200,
        attempt_id=attempt_for("monex", "130A"),
    )
    assert record_outcome(p, initial, outcome, observation()).entries[0].state == "success"
    active = initial
    with pytest.raises(ValueError, match="does not match its pair"):
        record_outcome(p, active, outcome, observation("rakuten"))
    with pytest.raises(ValueError, match="fixed outcome reason"):
        record_outcome(
            p,
            active,
            ExternalAssessmentCollectionOutcome(
                "monex",
                "130A",
                "parse_failure",
                "private raw exception",
                200,
                attempt_for("monex", "130A"),
            ),
        )
    with pytest.raises(ValueError, match="outside the fixed cohort"):
        record_outcome(
            p,
            active,
            ExternalAssessmentCollectionOutcome(
                "monex",
                "9432",
                "observed",
                http_status=200,
                attempt_id=attempt_for("monex", "9432"),
            ),
            observation(ticker="9432"),
        )
