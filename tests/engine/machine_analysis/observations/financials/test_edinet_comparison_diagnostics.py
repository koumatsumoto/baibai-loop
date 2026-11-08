"""比較captureの診断は原本読取・結果・失敗を変えず、集計だけを出す。"""

from io import StringIO

import pytest
from tests.engine.machine_analysis.observations.financials.test_edinet_comparison_native import (
    ByteConnection,
    fixed_archives,
)
from tests.engine.machine_analysis.observations.financials.test_edinet_comparison_reuse import (
    serialized,
)
from tests.engine.machine_analysis.observations.financials.test_edinet_comparisons import (
    ASOF,
    archive,
    metadata,
)

from baibai_engine.machine_analysis.observations.financials import edinet_comparison_reader as owner
from baibai_engine.storage.objects.r2 import LakePublishError


@pytest.mark.parametrize("route", ["native", "sql"])
@pytest.mark.parametrize("warm", [False, True])
def test_diagnostics_preserve_canonical_get_order_and_report_only_aggregates(
    monkeypatch, route, warm
):
    documents = [metadata(), metadata("S100YV6Y")]
    bodies = [(row["docID"], archive(row["docID"])) for row in documents]
    store, fixed = fixed_archives({(doc, 1): body for doc, body in bodies})
    monkeypatch.setattr(owner, "read_observed_documents", lambda *a, **k: documents)

    def read(*, seeds=(), diagnostics=None):
        store.get_keys.clear()
        result = owner.read_comparison_population(
            ByteConnection(bodies, forbidden=route == "native"),
            as_of=ASOF,
            archives=fixed if route == "native" else None,
            prior_observations=seeds,
            diagnostics=diagnostics,
        )
        return result, list(store.get_keys)

    cold, expected_gets = read()
    seeds = (
        [item.model_dump(mode="json") for rows in cold.values() for item in rows] if warm else []
    )
    # A synthetic clock gives exact durations without sleeping or external reads.
    clock = [0.0]
    monkeypatch.setattr(owner.time, "perf_counter", lambda: clock[0])
    original_get = store.get_bytes
    original_parse = owner.extract_comparisons

    def delayed_get(key):
        clock[0] += 0.25
        return original_get(key)

    def delayed_parse(**kwargs):
        clock[0] += 0.5
        return original_parse(**kwargs)

    def delayed_seeds():
        clock[0] += 0.125
        yield from seeds

    monkeypatch.setattr(store, "get_bytes", delayed_get)
    monkeypatch.setattr(owner, "extract_comparisons", delayed_parse)
    output = StringIO()
    result, actual_gets = read(seeds=delayed_seeds(), diagnostics=output)
    assert serialized(result) == serialized(cold)
    assert actual_gets == expected_gets
    assert output.getvalue().splitlines() == [
        (
            "model inputs: edinet_comparisons selected=2 ready=2 "
            f"seed_candidates={2 if warm else 0} seed_reused={2 if warm else 0} "
            f"parsed={0 if warm else 2} archive_read_count=2 "
            f"archive_read_bytes={sum(len(body) for _, body in bodies)} "
            f"archive_read_validate_seconds={'0.500' if route == 'native' else '0.000'} "
            "seed_read_validate_seconds=0.125 "
            f"parse_seconds={'0.000' if warm else '1.000'}"
        )
    ]


@pytest.mark.parametrize("failure", ["missing", "changed", "transport"])
def test_diagnostics_preserve_failure_and_stop_before_later_get(monkeypatch, failure):
    documents = [metadata(), metadata("S100YV6Y")]
    bodies = {(row["docID"], 1): archive(row["docID"]) for row in documents}
    store, fixed = fixed_archives(bodies)
    monkeypatch.setattr(owner, "read_observed_documents", lambda *a, **k: documents)
    original_get = store.get_bytes
    private_error = RuntimeError("private transport detail")

    def failed_get(key):
        original_get(key)
        if failure == "missing":
            raise KeyError("private object key")
        if failure == "changed":
            return b"private changed bytes"
        raise private_error

    monkeypatch.setattr(store, "get_bytes", failed_get)
    expected_error = RuntimeError if failure == "transport" else LakePublishError
    observed = []
    for diagnostics in (None, StringIO()):
        store.get_keys.clear()
        with pytest.raises(expected_error) as error:
            owner.read_comparison_population(
                ByteConnection([], forbidden=True),
                as_of=ASOF,
                archives=fixed,
                diagnostics=diagnostics,
            )
        observed.append((type(error.value), str(error.value), list(store.get_keys)))
        if failure == "transport":
            assert error.value is private_error
        if diagnostics is not None:
            assert diagnostics.getvalue() == ""
    assert observed[0] == observed[1]
    assert len(observed[0][2]) == 1


def test_blocked_original_is_counted_and_validated_without_consuming_seed(monkeypatch):
    document = {**metadata(), "xbrlFlag": "0"}
    body = b"blocked original private bytes"
    store, fixed = fixed_archives({(document["docID"], 1): body})
    monkeypatch.setattr(owner, "read_observed_documents", lambda *a, **k: [document])
    monkeypatch.setattr(owner.time, "perf_counter", lambda: 0.0)

    def forbidden_seed():
        pytest.fail("blocked inputs must not consume saved seed evidence")
        yield {}

    output = StringIO()
    result = owner.read_comparison_population(
        ByteConnection([], forbidden=True),
        as_of=ASOF,
        archives=fixed,
        prior_observations=forbidden_seed(),
        diagnostics=output,
    )
    assert result["436A"][0].reason == "comparison_document_unavailable"
    assert result["436A"][0].source_sha256 == fixed.metadata(document["docID"], 1).sha256
    assert len(store.get_keys) == 1
    assert output.getvalue() == (
        "model inputs: edinet_comparisons selected=1 ready=0 "
        "seed_candidates=0 seed_reused=0 parsed=0 archive_read_count=1 "
        f"archive_read_bytes={len(body)} archive_read_validate_seconds=0.000 "
        "seed_read_validate_seconds=0.000 parse_seconds=0.000\n"
    )
