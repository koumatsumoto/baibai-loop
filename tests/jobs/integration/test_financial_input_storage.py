"""保存入力は個社・毎日の全履歴コピーを作らず、cacheと独立して再生成できる。"""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.financial_native import (
    HEAD_KEY,
    PREFIX,
    FinancialAttempt,
    FinancialL1,
)
from baibai_engine.machine_analysis.generation.financial_inputs import (
    INPUT_PREFIX,
    read_financial_input,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.objects.r2 import LakePublishError, R2Error
from baibai_jobs.integration.financial_native import model_input, rebuild_saved_input

AT = datetime(2026, 11, 4, tzinfo=UTC)


def attempt(identity, day, *, at=AT, partial=False):
    return FinancialAttempt(
        identity,
        day,
        at,
        (
            JQuantsPage(
                "financial_summaries",
                {"date": day.isoformat()},
                at,
                b'{"data":[],"pagination_key":"more"}' if partial else b'{"data":[]}',
            ),
        ),
    )


def selection(store, identity):
    digest = identity.descriptor["selection_sha256"]
    return json.loads(store.get_bytes(INPUT_PREFIX + "selections/" + digest + ".json"))


def test_monthly_inputs_reuse_history_and_never_copy_body_or_derived_daily_rows(tmp_path):
    store = MemoryR2Store()
    owner = FinancialL1(store, tmp_path / "native")
    owner.initialize()
    jan, feb = date(2026, 1, 31), date(2026, 2, 1)
    owner.commit([attempt("jan", jan), attempt("feb", feb)])
    fixed = owner.fixed()
    first = model_input(store, fixed, as_of=feb, evaluated_at=AT).identity
    root = selection(store, first)
    assert set(root["months"]) == {"2026-01", "2026-02"}
    assert set(first.descriptor) == {
        "format",
        "head_sha256",
        "as_of",
        "selection_sha256",
        "summary",
    }
    assert len(first.model_dump_json()) < 2500
    assert all(
        "receipt_status" not in value.body.decode() and '"pages"' not in value.body.decode()
        for key, value in store.values.items()
        if key.startswith(INPUT_PREFIX)
    )
    store.put_keys.clear()
    assert model_input(store, fixed, as_of=feb, evaluated_at=AT).identity == first
    assert store.put_keys == []
    # A new missing day only extends the closed range, without rewriting months.
    later = model_input(store, fixed, as_of=feb + timedelta(days=1), evaluated_at=AT).identity
    assert len(store.put_keys) == 1
    assert "/selections/" in store.put_keys[0]
    assert selection(store, later)["months"] == root["months"]
    assert read_financial_input(store, later.descriptor, days=(feb + timedelta(days=1),)) == {
        "scopes": {"2026-02-02": {}},
        "receipts": {},
    }
    # Equal source values change confirmation metadata, not retained source bodies.
    original_packs = {key for key in store.values if key.startswith(PREFIX + "packs/")}
    owner.commit([attempt("equal", feb, at=AT + timedelta(hours=1))])
    assert {key for key in store.values if key.startswith(PREFIX + "packs/")} == original_packs
    store.put_keys.clear()
    latest = model_input(
        store, owner.fixed(), as_of=feb, evaluated_at=AT + timedelta(hours=1)
    ).identity
    latest_root = selection(store, latest)
    assert latest_root["months"]["2026-01"] == root["months"]["2026-01"]
    assert latest_root["months"]["2026-02"] != root["months"]["2026-02"]
    assert len(store.put_keys) == 2  # only changed month + small root
    assert (
        read_financial_input(store, first.descriptor, days=(feb,))["scopes"][feb.isoformat()][
            "confirmed_at"
        ]
        == AT.isoformat()
    )
    assert (
        read_financial_input(store, latest.descriptor, days=(feb,))["scopes"][feb.isoformat()][
            "confirmed_at"
        ]
        == (AT + timedelta(hours=1)).isoformat()
    )


def test_cold_input_retains_failed_held_missing_states_and_reads_only_requested_month(tmp_path):
    store = MemoryR2Store()
    owner = FinancialL1(store, tmp_path / "native")
    owner.initialize()
    jan, feb = date(2026, 1, 31), date(2026, 2, 1)
    owner.commit([attempt("jan", jan), attempt("feb", feb)])
    owner.commit([attempt("failed", feb, at=AT + timedelta(hours=1), partial=True)])
    owner.commit([attempt("held", feb, at=AT + timedelta(hours=1))])
    fixed = owner.fixed()
    saved = model_input(
        store, fixed, as_of=feb + timedelta(days=1), evaluated_at=AT + timedelta(hours=1)
    ).identity
    root = selection(store, saved)
    del store.values[HEAD_KEY]
    store.get_keys.clear()
    result = read_financial_input(store, saved.descriptor, days=(feb, feb + timedelta(days=1)))
    assert result["scopes"][feb.isoformat()]["failure"] == "failed"
    assert result["scopes"][feb.isoformat()]["held"] == "held"
    assert result["scopes"]["2026-02-02"] == {}
    assert set(result["receipts"]) == {"feb", "failed", "held"}
    assert HEAD_KEY not in store.get_keys
    assert not any(root["months"]["2026-01"] in key for key in store.get_keys)
    report = rebuild_saved_input(
        store, saved, start=feb, end=feb + timedelta(days=1), output=tmp_path / "cold"
    )
    assert report == {"days": 2, "rows": 0, "unavailable": 1, "partial": 0, "incomplete": 1}
    metadata = json.loads((tmp_path / "cold" / "input.json").read_bytes())
    assert metadata["acquisition"] == result["scopes"]
    assert metadata["receipts"] == result["receipts"]
    assert "pages" not in canonical_json(metadata)
    with pytest.raises(ValueError, match="outside the saved"):
        read_financial_input(store, saved.descriptor, days=(jan - timedelta(days=1),))


@pytest.mark.parametrize("kind", ["months", "selections"])
@pytest.mark.parametrize("missing", [False, True])
def test_missing_or_corrupt_fixed_objects_fail_without_latest_fallback(tmp_path, kind, missing):
    store = MemoryR2Store()
    owner = FinancialL1(store, tmp_path / "native")
    owner.initialize()
    day = date(2026, 2, 1)
    owner.commit([attempt("feb", day)])
    saved = model_input(store, owner.fixed(), as_of=day, evaluated_at=AT).identity
    key = next(key for key in store.values if key.startswith(INPUT_PREFIX + kind + "/"))
    if missing:
        del store.values[key]
    else:
        store.store_object(key, b"{}")
    store.get_keys.clear()
    with pytest.raises(LakePublishError, match=r"unavailable|integrity"):
        rebuild_saved_input(store, saved, start=day, end=day, output=tmp_path / "failed")
    assert HEAD_KEY not in store.get_keys
    assert not (tmp_path / "failed").exists()


def test_legacy_inline_input_rebuilds_without_any_shared_input_objects(tmp_path):
    store = MemoryR2Store()
    owner = FinancialL1(store, tmp_path / "native")
    owner.initialize()
    day = date(2026, 2, 1)
    earlier = date(2026, 1, 31)
    owner.commit([attempt("jan", earlier), attempt("feb", day)])
    saved = model_input(store, owner.fixed(), as_of=day, evaluated_at=AT).identity
    descriptor = {
        **read_financial_input(store, saved.descriptor, days=(earlier, day)),
        "head_sha256": owner.fixed().content_id,
    }
    saved = type(saved)(
        kind=saved.kind,
        reference="legacy",
        sha256=hashlib.sha256(canonical_json(descriptor).encode()).hexdigest(),
        descriptor=descriptor,
    )
    for key in tuple(store.values):
        if key.startswith(INPUT_PREFIX) or key == HEAD_KEY:
            del store.values[key]
    assert (
        rebuild_saved_input(store, saved, start=day, end=day, output=tmp_path / "legacy")["days"]
        == 1
    )
    metadata = json.loads((tmp_path / "legacy" / "input.json").read_bytes())
    assert set(metadata["acquisition"]) == {day.isoformat()}
    assert set(metadata["receipts"]) == {"feb"}


class LostAcknowledgement(MemoryR2Store):
    fail = False
    uploaded = False

    def put_file(self, key, path, **kwargs):
        if self.fail and key.startswith(INPUT_PREFIX):
            if self.uploaded:
                super().put_file(key, path, **kwargs)
            raise R2Error("lost acknowledgement")
        return super().put_file(key, path, **kwargs)


@pytest.mark.parametrize("uploaded", [False, True])
def test_fixed_input_write_failure_never_changes_source_head(tmp_path, uploaded):
    store = LostAcknowledgement()
    owner = FinancialL1(store, tmp_path / "native")
    owner.initialize()
    day = date(2026, 2, 1)
    owner.commit([attempt("feb", day)])
    before = owner.fixed()
    store.fail, store.uploaded = True, uploaded
    if uploaded:
        saved = model_input(store, before, as_of=day, evaluated_at=AT).identity
        assert read_financial_input(store, saved.descriptor, days=(day,))["scopes"]
    else:
        with pytest.raises(R2Error, match="lost acknowledgement"):
            model_input(store, before, as_of=day, evaluated_at=AT)
    assert owner.fixed().payload == before.payload
