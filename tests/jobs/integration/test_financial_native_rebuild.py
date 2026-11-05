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
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.objects.r2 import LakePublishError

DAY = date(2026, 11, 2)
AT = datetime(2026, 11, 3, tzinfo=UTC)


def attempt(identity, value=1, *, at=AT, partial=False):
    body = json.dumps(
        {
            "data": [{"Code": "13010", "DiscDate": DAY.isoformat(), "DiscNo": "1", "Sales": value}],
            **({"pagination_key": "next"} if partial else {}),
        }
    ).encode()
    return FinancialAttempt(
        identity,
        DAY,
        at,
        (JQuantsPage("financial_summaries", {"date": DAY.isoformat()}, at, body),),
    )


def test_saved_input_cli_rebuilds_same_rows_and_shortages_without_latest_head(
    tmp_path, monkeypatch
):
    store = MemoryR2Store()
    store.close = lambda: None
    owner = FinancialL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit([attempt("first"), attempt("partial", at=AT + timedelta(hours=1), partial=True)])
    fixed = owner.fixed()
    end = DAY + timedelta(days=2)
    saved = job.model_input(store, fixed, as_of=end, evaluated_at=AT + timedelta(hours=1)).identity
    job.rebuild(store, fixed, start=DAY, end=end, output=tmp_path / "baseline")
    owner.commit([attempt("later", value=2, at=AT + timedelta(hours=2))])
    assert owner.fixed().payload != fixed.payload
    del store.values[HEAD_KEY]
    file = tmp_path / "saved-input.json"
    file.write_text(saved.model_dump_json())
    monkeypatch.setattr(job, "Boto3R2Store", lambda **kw: store)
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
    raw = saved.model_dump(mode="json")
    raw["descriptor"]["receipts"]["first"]["fingerprint"] = "f" * 64
    raw["sha256"] = hashlib.sha256(canonical_json(raw["descriptor"]).encode()).hexdigest()
    altered = InputSnapshot.model_validate(raw)
    with pytest.raises(LakePublishError, match="receipt differs"):
        job.rebuild_saved_input(
            store, altered, start=DAY, end=DAY, output=tmp_path / "changed-fingerprint"
        )
