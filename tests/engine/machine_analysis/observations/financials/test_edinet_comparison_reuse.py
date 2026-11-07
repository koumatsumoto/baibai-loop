"""任意seedはXML解析だけを省き、今回の原本・文書選択を置き換えない。"""

import copy
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from tests.engine.machine_analysis.observations.financials.test_edinet_comparison_native import (
    ByteConnection,
    correction,
    fixed_archives,
)
from tests.engine.machine_analysis.observations.financials.test_edinet_comparisons import (
    ASOF,
    archive,
    metadata,
)

from baibai_engine.machine_analysis.observations.financials import edinet_comparison_reader as owner
from baibai_engine.machine_analysis.observations.financials import edinet_comparisons as extraction
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.objects.r2 import LakePublishError


def serialized(population):
    return canonical_json(
        {
            ticker: [item.model_dump(mode="json") for item in items]
            for ticker, items in population.items()
        }
    )


@pytest.fixture
def read(monkeypatch):
    counts = {"extract": 0, "xml": 0}
    original_extract = owner.extract_comparisons
    original_instance = extraction.read_instance

    def counted_extract(**kwargs):
        counts["extract"] += 1
        return original_extract(**kwargs)

    def counted_instance(*args, **kwargs):
        counts["xml"] += 1
        return original_instance(*args, **kwargs)

    monkeypatch.setattr(owner, "extract_comparisons", counted_extract)
    monkeypatch.setattr(extraction, "read_instance", counted_instance)

    def run(documents, bodies, *, seeds=(), route="native", fixed=None, **kwargs):
        counts.update(extract=0, xml=0)
        store, archives = fixed or fixed_archives({(doc, 1): body for doc, body in bodies})
        store.get_keys.clear()
        monkeypatch.setattr(owner, "read_observed_documents", lambda *a, **k: documents)
        result = owner.read_comparison_population(
            ByteConnection(bodies, forbidden=route == "native"),
            as_of=kwargs.pop("as_of", ASOF),
            archives=archives if route == "native" else None,
            prior_observations=seeds,
            **kwargs,
        )
        return result, dict(counts), store.get_keys

    return run


@pytest.mark.parametrize("route", ["native", "sql"])
@pytest.mark.parametrize("day_change", [0, 1])
def test_success_seed_skips_xml_but_keeps_original_get_and_exact_native_evidence(
    read, route, day_change
):
    bodies = [("S100YRSR", archive()), ("S100YV6Y", archive("S100YV6Y"))]
    documents = [metadata(), metadata("S100YV6Y")]
    cold, counts, gets = read(documents, bodies, route=route)
    assert counts == {"extract": 2, "xml": 2}
    assert len(gets) == (2 if route == "native" else 0)
    seeds = [item.model_dump(mode="json") for items in cold.values() for item in items]
    warm, counts, gets = read(
        documents,
        bodies,
        route=route,
        seeds=iter(seeds),
        as_of=ASOF + timedelta(days=day_change),
        known_at=datetime(2026, 10, 31, tzinfo=UTC),
    )
    assert counts == {"extract": 0, "xml": 0}
    assert len(gets) == (2 if route == "native" else 0)
    assert serialized(warm) == serialized(cold)
    assert warm["436A"][0].pairs[0].current.amount == "3525761000"
    assert warm["436A"][0].pairs[0].current.decimals == "-3"


@pytest.mark.parametrize(
    "damage",
    [
        "revision_missing",
        "revision_old",
        "sha_missing",
        "sha_changed",
        "ticker",
        "doc_id",
        "submitted",
        "document_type",
        "unknown_field",
        "missing_default",
        "missing_operand_default",
        "malformed_amount",
        "invalid_decimal",
        "nan",
        "infinity",
        "empty_pairs",
        "missing_metric",
        "unknown_basis",
        "unknown_period",
        "future_period",
        "inconsistent_period",
        "missing_locator",
        "prior_period",
        "missing_pair_without_reason",
        "complete_pair_with_reason",
        "issuer_mismatch",
        "reason",
        "duplicate",
    ],
)
def test_invalid_or_ambiguous_seed_falls_back_to_exact_cold_parse(read, damage):
    bodies = [("S100YRSR", archive())]
    cold, _, _ = read([metadata()], bodies)
    seed = cold["436A"][0].model_dump(mode="json")
    if damage == "revision_missing":
        del seed["extractor_revision"]
    elif damage == "revision_old":
        seed["extractor_revision"] = "old"
    elif damage == "sha_missing":
        seed["source_sha256"] = None
    elif damage == "sha_changed":
        seed["source_sha256"] = "0" * 64
    elif damage == "ticker":
        seed["ticker"] = "1111"
    elif damage == "doc_id":
        seed["source_doc_id"] = "S100OTHER"
    elif damage == "submitted":
        seed["source_submit_datetime"] = "2026-07-26T15:31:00+09:00"
    elif damage == "document_type":
        seed["document_type"] = "160"
    elif damage == "unknown_field":
        seed["unknown"] = "untrusted"
    elif damage == "missing_default":
        del seed["pairs"]
    elif damage == "missing_operand_default":
        del seed["pairs"][0]["current"]["currency"]
    elif damage == "malformed_amount":
        seed["pairs"][0]["current"]["amount"] = {"number": 3525761000}
    elif damage in {"invalid_decimal", "nan", "infinity"}:
        seed["pairs"][0]["current"]["amount"] = {
            "invalid_decimal": "not-a-number",
            "nan": "NaN",
            "infinity": "Infinity",
        }[damage]
    elif damage == "empty_pairs":
        seed["pairs"] = []
    elif damage == "missing_metric":
        seed["pairs"].pop()
    elif damage == "unknown_basis":
        seed["statement_basis"] = None
    elif damage == "unknown_period":
        seed["current_period_end"] = None
    elif damage == "future_period":
        seed["current_period_end"] = "2027-04-30"
    elif damage == "inconsistent_period":
        seed["pairs"][0]["current"]["period_end"] = "2025-04-30"
    elif damage == "missing_locator":
        seed["pairs"][0]["current"]["locators"] = []
    elif damage == "prior_period":
        seed["pairs"][0]["comparative"]["period_end"] = "2023-04-30"
    elif damage == "missing_pair_without_reason":
        seed["pairs"][0]["current"] = None
    elif damage == "complete_pair_with_reason":
        seed["pairs"][0]["reason"] = "comparison_operand_not_observed"
    elif damage == "issuer_mismatch":
        seed["pairs"][0]["comparative"]["issuer_id"] = "different-issuer"
    elif damage == "reason":
        seed["reason"] = "comparison_document_unavailable"
    seeds = [seed, copy.deepcopy(seed)] if damage == "duplicate" else [seed]
    result, counts, gets = read([metadata()], bodies, seeds=seeds)
    assert counts == {"extract": 1, "xml": 1}
    assert len(gets) == 1
    assert serialized(result) == serialized(cold)


@pytest.mark.parametrize("cutoff", ["asof", "known_at", "ticker"])
def test_fresh_publication_and_subject_cutoffs_do_not_consume_seed_or_body(read, cutoff):
    bodies = [("S100YRSR", archive())]
    cold, _, _ = read([metadata()], bodies)
    stamp = cold["436A"][0].source_submit_datetime

    def forbidden_seed():
        pytest.fail("no eligible document needs optional saved evidence")
        yield {}  # pragma: no cover

    kwargs = (
        {"as_of": stamp.date() - timedelta(days=1)}
        if cutoff == "asof"
        else {"known_at": stamp - timedelta(microseconds=1)}
        if cutoff == "known_at"
        else {"tickers": ("1111",)}
    )
    result, counts, gets = read([metadata()], bodies, seeds=forbidden_seed(), **kwargs)
    assert result == {}
    assert counts == {"extract": 0, "xml": 0}
    assert not gets


@pytest.mark.parametrize("blocked", ["unavailable", "withdrawn", "quarantined", "orphan"])
def test_current_document_reason_overrides_old_success_without_skipping_get(read, blocked):
    original = metadata()
    target = correction() if blocked == "orphan" else original
    bodies = [(target["docID"], archive())]
    cold, _, _ = read([original, target], bodies)
    seeds = [value.model_dump(mode="json") for value in cold["436A"]]
    if blocked == "unavailable":
        documents = [{**original, "xbrlFlag": "0"}]
        reason = "comparison_document_unavailable"
    elif blocked == "withdrawn":
        documents = [
            original,
            {
                **original,
                "docID": "S100EVENT",
                "parentDocID": original["docID"],
                "withdrawalStatus": "1",
                "opeDateTime": "2026-07-26 15:31",
            },
        ]
        reason = "comparison_document_unavailable"
    elif blocked == "quarantined":
        documents = [original, {**correction("S100LOST"), "docInfoEditStatus": "1"}]
        reason = "comparison_document_events_unresolved"
    else:
        documents = [correction(parent="S100LOST")]
        reason = "comparison_correction_ancestry_unknown"
    result, counts, gets = read(documents, bodies, seeds=seeds)
    assert counts == {"extract": 0, "xml": 0}
    assert len(gets) == 1
    assert result["436A"][0].reason == reason
    assert not result["436A"][0].pairs
    assert result["436A"][0].source_sha256 == hashlib.sha256(archive()).hexdigest()


def test_parent_and_family_membership_are_recomputed_from_current_documents(read):
    bodies = [("S100FIX", archive())]
    cold, _, _ = read([metadata(), correction()], bodies)
    seeds = [value.model_dump(mode="json") for value in cold["436A"]]
    documents = [
        {**metadata(), "docID": "S100OTHER"},
        correction(parent="S100OTHER"),
    ]
    result, counts, gets = read(documents, bodies, seeds=seeds)
    assert counts == {"extract": 0, "xml": 0}
    assert len(gets) == 1
    assert [value.source_doc_id for value in result["436A"]] == ["S100OTHER", "S100FIX"]
    assert result["436A"][0].reason == "comparison_archive_not_received"
    assert result["436A"][1].parent_doc_id == "S100OTHER"
    assert result["436A"][1].pairs == cold["436A"][1].pairs


@pytest.mark.parametrize("damage", ["missing", "changed"])
@pytest.mark.parametrize("blocked", [False, True])
def test_warm_seed_cannot_hide_missing_or_changed_retained_native_body(read, damage, blocked):
    body = archive()
    bodies = [("S100YRSR", body)]
    cold, _, _ = read([metadata()], bodies)
    store, fixed = fixed_archives({("S100YRSR", 1): body})
    key = f"l1/edinet/archives/v1/objects/{hashlib.sha256(body).hexdigest()}.zip"
    if damage == "missing":
        del store.values[key]
    else:
        store.store_object(key, b"changed original")
    document = {**metadata(), "xbrlFlag": "0"} if blocked else metadata()
    with pytest.raises(LakePublishError, match="missing or changed"):
        read(
            [document],
            bodies,
            seeds=[cold["436A"][0].model_dump(mode="json")],
            fixed=(store, fixed),
        )
    assert store.get_keys == [key]


@pytest.mark.parametrize("change", ["amount", "future_period", "non_ifrs"])
def test_changed_fixed_body_is_parsed_even_when_old_success_exists(read, change):
    cold, _, _ = read([metadata()], [("S100YRSR", archive())])
    replacement = {
        "amount": {b"3525761000": b"3525761001"},
        "future_period": {b"2026-04-30": b"2027-04-30"},
        "non_ifrs": {b">IFRS<": b">JP<"},
    }[change]
    bodies = [("S100YRSR", archive(replace=replacement))]
    expected, _, _ = read([metadata()], bodies)
    result, counts, gets = read(
        [metadata()], bodies, seeds=[cold["436A"][0].model_dump(mode="json")]
    )
    assert counts == {"extract": 1, "xml": 1}
    assert len(gets) == 1
    assert serialized(result) == serialized(expected)
    if change == "amount":
        assert result["436A"][0].pairs[0].current.amount == "3525761001"
    else:
        assert result["436A"][0].reason is not None


def test_same_zip_does_not_share_success_across_ticker_or_document_identity(read):
    body = archive()
    cold, _, _ = read([metadata()], [("S100YRSR", body)])
    other = {**metadata(), "docID": "S100OTHER", "secCode": "11110"}
    documents = [metadata(), other]
    bodies = [("S100YRSR", body), ("S100OTHER", body)]
    result, counts, gets = read(documents, bodies, seeds=[cold["436A"][0].model_dump(mode="json")])
    assert counts == {"extract": 1, "xml": 1}
    assert len(gets) == 2
    assert result["436A"] == cold["436A"]
    assert result["1111"][0].reason == "comparison_security_mismatch"


def test_parse_failure_is_not_a_success_seed(read):
    bodies = [("S100YRSR", archive(replace={b">IFRS<": b">JP<"}))]
    cold, _, _ = read([metadata()], bodies)
    result, counts, gets = read(
        [metadata()], bodies, seeds=[cold["436A"][0].model_dump(mode="json")]
    )
    assert counts == {"extract": 1, "xml": 1}
    assert len(gets) == 1
    assert result == cold


@pytest.mark.parametrize("partial", ["nil", "missing", "conflict"])
def test_legitimate_partial_operands_remain_reusable_with_exact_output(read, partial):
    from baibai_engine.machine_analysis.models.company_growth.calculations import (
        build_reported_comparison,
    )

    fact = (
        b'<jpigp_cor:NetSalesIFRS contextRef="CurrentYearDuration_NonConsolidatedMember" '
        b'decimals="-3" unitRef="JPY">3525761000</jpigp_cor:NetSalesIFRS>'
    )
    replacement = {
        "nil": fact.replace(b'unitRef="JPY">3525761000', b'unitRef="JPY" xsi:nil="true">'),
        "missing": b"",
        "conflict": fact + fact.replace(b"3525761000", b"3525761001"),
    }[partial]
    bodies = [("S100YRSR", archive(replace={fact: replacement}))]
    cold, counts, _ = read([metadata()], bodies)
    assert counts == {"extract": 1, "xml": 1}
    before = cold["436A"][0]
    assert before.reason is None
    assert before.pairs[0].reason is not None
    result, counts, gets = read([metadata()], bodies, seeds=[before.model_dump(mode="json")])
    assert counts == {"extract": 0, "xml": 0}
    assert len(gets) == 1
    assert serialized(result) == serialized(cold)
    assert build_reported_comparison(result["436A"][0]) == build_reported_comparison(before)
