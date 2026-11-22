"""A saved generation's financial input can be rebuilt after mutable heads disappear."""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta

import pyarrow.parquet as pq
import pytest
from tests.helpers.r2_store import MemoryR2Store

import baibai_jobs.integration.financial_native as job
from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.financial_native import (
    HEAD_KEY,
    FinancialAttempt,
    FinancialL1,
)
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.financial_inputs import read_financial_input
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.objects.r2 import LakePublishError

DAY = date(2026, 11, 2)
AT = datetime(2026, 11, 3, tzinfo=UTC)


def attempt(identity, value=1, *, at=AT, partial=False, extra=()):
    body = json.dumps(
        {
            "data": [
                {"Code": "13010", "DiscDate": DAY.isoformat(), "DiscNo": "1", "Sales": value},
                *extra,
            ],
            **({"pagination_key": "next"} if partial else {}),
        }
    ).encode()
    return FinancialAttempt(
        identity,
        DAY,
        at,
        (JQuantsPage("financial_summaries", {"date": DAY.isoformat()}, at, body),),
    )


def snapshot_from_descriptor(descriptor):
    digest = hashlib.sha256(canonical_json(descriptor).encode()).hexdigest()
    return InputSnapshot(
        kind="native_l1",
        reference="jquants-financial-" + digest,
        sha256=digest,
        descriptor=descriptor,
    )


@pytest.mark.parametrize("query_profile", ["", "small-runner"])
def test_saved_input_cli_rebuilds_same_rows_and_shortages_without_latest_head(
    tmp_path, monkeypatch, mocker, query_profile
):
    monkeypatch.setenv("BAIBAI_LAKE_QUERY_PROFILE", query_profile)
    store = MemoryR2Store()
    store.close = lambda: None
    owner = FinancialL1(store, tmp_path / "native")
    owner.initialize()
    first = attempt(
        "first",
        extra=(
            {"Code": True, "DiscDate": DAY.isoformat(), "DiscNo": "bad-code"},
            {"Code": "13010", "DiscDate": f"{DAY}junk", "DiscNo": "bad-date"},
            {"Code": "13011", "DiscDate": DAY.isoformat(), "DiscNo": "secondary"},
        ),
    )
    owner.commit([first, attempt("partial", at=AT + timedelta(hours=1), partial=True)])
    fixed = owner.fixed()
    end = DAY + timedelta(days=2)
    original_objects = {key: item.body for key, item in store.values.items()}
    observed = job.model_input(store, fixed, as_of=end, evaluated_at=AT + timedelta(hours=1))
    saved = observed.identity
    assert [(row.ticker, row.disclosed_at) for row in observed.documents] == [("1301", DAY)]
    assert saved.descriptor["summary"]["partial_days"] == 1
    assert next(fixed.requests(store, days=(DAY,))).request.pages == first.pages
    assert all(store.values[key].body == body for key, body in original_objects.items())
    job.rebuild(store, fixed, start=DAY, end=end, output=tmp_path / "baseline")
    raw_rebuild = json.loads((tmp_path / "baseline" / "input.json").read_bytes())
    assert raw_rebuild["projection_revision"] == saved.descriptor["summary"]["projection_revision"]
    assert "saved_input" not in raw_rebuild
    owner.commit([attempt("later", value=2, at=AT + timedelta(hours=2))])
    assert owner.fixed().payload != fixed.payload
    del store.values[HEAD_KEY]
    file = tmp_path / "saved-input.json"
    file.write_text(saved.model_dump_json())
    monkeypatch.setattr(job, "Boto3R2Store", lambda **kw: store)
    parse = mocker.spy(InputSnapshot, "model_validate_json")
    store.get_keys.clear()
    assert (
        job.main(
            [
                "--saved-input",
                str(file),
                "--start",
                DAY.isoformat(),
                "--end",
                end.isoformat(),
                "--work-directory",
                str(tmp_path / "fresh"),
                "--output",
                str(tmp_path / "rebuilt"),
            ]
        )
        == 0
    )
    parse.assert_called_once_with(file.read_bytes())
    assert HEAD_KEY not in store.get_keys
    for table in ("financial_summaries", "financial_receipt_status"):
        assert (
            pq.read_table(tmp_path / "rebuilt" / (table + ".parquet")).to_pylist()
            == pq.read_table(tmp_path / "baseline" / (table + ".parquet")).to_pylist()
        )
    assert not (tmp_path / "rebuilt" / "head.json").exists()
    metadata = json.loads((tmp_path / "rebuilt" / "input.json").read_bytes())
    assert metadata["source_head_sha256"] == fixed.content_id
    assert metadata["saved_input"] == saved.model_dump(mode="json")
    status = next(
        row
        for row in pq.read_table(tmp_path / "rebuilt/financial_receipt_status.parquet").to_pylist()
        if row["day"] == DAY
    )
    assert (status["projection_status"], status["rejected_count"], status["excluded_count"]) == (
        "partial",
        2,
        1,
    )
    # Changing a saved descriptor without its matching identity never falls back.
    raw = json.loads(file.read_bytes())
    raw["descriptor"]["head_sha256"] = "f" * 64
    file.write_text(json.dumps(raw))
    store.get_keys.clear()
    assert (
        job.main(
            [
                "--saved-input",
                str(file),
                "--start",
                DAY.isoformat(),
                "--end",
                end.isoformat(),
                "--work-directory",
                str(tmp_path / "fresh"),
                "--output",
                str(tmp_path / "invalid"),
            ]
        )
        == 1
    )
    assert store.get_keys == []


@pytest.mark.parametrize(
    "kind", ["compact-unversioned", "inline-unversioned", "other-revision", "foreign"]
)
def test_finalized_financial_replay_rejects_other_recipes_before_io(
    tmp_path, kind, monkeypatch, capsys
):
    store = MemoryR2Store()
    owner = FinancialL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit([attempt("first")])
    current = job.model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT).identity
    descriptor = current.model_dump(mode="json")["descriptor"]
    message = "projection revision"
    if kind == "inline-unversioned":
        fixed_fields = read_financial_input(store, descriptor, days=(DAY,))
        descriptor.update(
            fixed_fields, receipt_status={DAY.isoformat(): {"status": "ok", "incomplete": 0}}
        )
        del descriptor["selection_sha256"], descriptor["format"]
    if kind in {"compact-unversioned", "inline-unversioned"}:
        descriptor["summary"].pop("projection_revision", None)
    elif kind == "other-revision":
        descriptor["summary"]["projection_revision"] = "different"
    else:
        descriptor["format"] = "jquants-price-input-v2"
        message = "unsupported fixed financial input format"
    saved = snapshot_from_descriptor(descriptor)
    store.get_keys.clear()
    store.head_keys.clear()
    store.put_keys.clear()
    output = tmp_path / "rejected"
    with pytest.raises(ValueError, match=message):
        job.rebuild_saved_input(store, saved, start=DAY, end=DAY, output=output)
    assert store.get_keys == store.head_keys == store.put_keys == []
    assert not output.exists()
    if kind == "compact-unversioned":
        file = tmp_path / "unversioned.json"
        file.write_text(saved.model_dump_json())
        monkeypatch.setattr(job, "Boto3R2Store", lambda **_: pytest.fail("configured an R2 store"))
        assert (
            job.main(
                [
                    "--saved-input",
                    str(file),
                    "--start",
                    DAY.isoformat(),
                    "--end",
                    DAY.isoformat(),
                    "--work-directory",
                    str(tmp_path / "work"),
                    "--output",
                    str(output),
                ]
            )
            == 1
        )
        assert "verified producer commit" in capsys.readouterr().err
        assert store.get_keys == store.head_keys == store.put_keys == []
        assert not output.exists()


def test_saved_input_verifies_pack_fingerprint_and_does_not_expand_its_scope(tmp_path):
    store = MemoryR2Store()
    owner = FinancialL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit([attempt("first")])
    saved = job.model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT).identity
    with pytest.raises(ValueError, match="outside the saved"):
        job.rebuild_saved_input(
            store, saved, start=DAY - timedelta(days=1), end=DAY, output=tmp_path / "outside"
        )
    raw = {**saved.descriptor, **read_financial_input(store, saved.descriptor, days=(DAY,))}
    raw["receipts"]["first"]["fingerprint"] = "f" * 64
    # The tagged descriptor and its month/root remain valid; only the receipt fingerprint differs.
    altered = snapshot_from_descriptor(job.retain_financial_input(store, raw))
    with pytest.raises(LakePublishError, match="receipt differs"):
        job.rebuild_saved_input(
            store, altered, start=DAY, end=DAY, output=tmp_path / "changed-fingerprint"
        )


def test_constructed_financial_input_is_revalidated_before_reading_selection(tmp_path):
    store = MemoryR2Store()
    owner = FinancialL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit([attempt("first")])
    saved = job.model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT).identity
    changed = saved.model_copy(update={"descriptor": {**saved.descriptor, "as_of": "2026-11-03"}})
    store.get_keys.clear()
    output = tmp_path / "tampered"
    with pytest.raises(ValueError, match="digest-matching fixed descriptor"):
        job.rebuild_saved_input(store, changed, start=DAY, end=DAY, output=output)
    assert not store.get_keys
    assert not output.exists()
