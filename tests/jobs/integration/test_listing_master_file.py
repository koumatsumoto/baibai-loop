"""Local commands and checked R2 use exactly the same current-listing codec."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.jobs.integration.test_listing_master_rakuten import (
    _CUTOFF,
    _DAY,
    _bindings,
    _snapshot,
    _Store,
    _store,
)

from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    LISTING_MASTER_MAX_BYTES,
    CurrentListingMarket,
    decode_current_listing_master,
    encode_current_listing_master,
    read_local_listing_master,
)
from baibai_jobs.integration.listing_master_current import (
    LISTING_MASTER_CURRENT_KEY,
    read_current_listing_master,
    refresh_current_us_rakuten_scope,
)


def _mixed() -> _Store:
    store = _store()
    snapshot = _snapshot()
    assert (
        refresh_current_us_rakuten_scope(
            store,
            snapshot=snapshot,
            bindings=_bindings(store, snapshot),
            expected_session=_DAY,
            cutoff=_CUTOFF,
        )
        == "updated"
    )
    return store


def test_checked_r2_and_local_current_file_use_the_same_types_and_exact_bytes(
    tmp_path: Path,
) -> None:
    store = _mixed()
    body = store.values[LISTING_MASTER_CURRENT_KEY].body
    checked = read_current_listing_master(store)
    assert tuple(checked) == ("JP", "US")
    assert isinstance(checked["JP"], CurrentListingMarket)
    anchor = checked["US"].rakuten
    assert anchor is not None
    assert len(anchor.active_listing_sha256) == 64
    target = tmp_path / "current.json"
    target.write_bytes(body)

    assert read_local_listing_master(target) == checked
    assert decode_current_listing_master(body) == checked
    assert encode_current_listing_master({"US": checked["US"], "JP": checked["JP"]}) == body
    assert target.read_bytes() == body
    assert len(store.put_keys) == 3


def test_single_market_section_never_invents_the_other_market(tmp_path: Path) -> None:
    store = _mixed()
    current = read_current_listing_master(store)
    target = tmp_path / "current.json"
    target.write_bytes(encode_current_listing_master({"US": current["US"]}))
    assert read_local_listing_master(target) == {"US": current["US"]}
    with pytest.raises(FileNotFoundError):
        read_local_listing_master(target.with_name("explicitly-missing.json"))


@pytest.mark.parametrize(
    "damage",
    [
        "wrong-market",
        "unknown-nested-field",
        "missing-broker-anchor",
        "invalid-broker-anchor",
        "empty",
    ],
)
def test_local_and_remote_reject_a_damaged_explicit_current_file(
    tmp_path: Path, damage: str
) -> None:
    store = _mixed()
    value = json.loads(store.values[LISTING_MASTER_CURRENT_KEY].body)
    if damage == "wrong-market":
        value["US"] = value["JP"]
    elif damage == "unknown-nested-field":
        value["JP"]["slice"]["rows"][0]["new_unrecognized_metadata"] = "must not be discarded"
    elif damage == "missing-broker-anchor":
        del value["US"]["rakuten"]["active_listing_sha256"]
    elif damage == "invalid-broker-anchor":
        value["US"]["rakuten"]["active_listing_sha256"] = "invalid"
    else:
        value = {}

    body = json.dumps(value).encode("utf-8")
    store.store_object(LISTING_MASTER_CURRENT_KEY, body)
    target = tmp_path / "current.json"
    target.write_bytes(body)

    message = {
        "wrong-market": "does not match its market sections",
        "unknown-nested-field": "new_unrecognized_metadata",
        "missing-broker-anchor": "active_listing_sha256",
        "invalid-broker-anchor": "invalid current Rakuten scope",
        "empty": "does not match its market sections",
    }[damage]
    with pytest.raises(ValueError, match=message):
        read_current_listing_master(store)
    with pytest.raises(ValueError, match=message):
        read_local_listing_master(target)


def test_local_read_is_bounded_before_json_validation(tmp_path: Path) -> None:
    target = tmp_path / "current.json"
    with target.open("wb") as stream:
        stream.truncate(LISTING_MASTER_MAX_BYTES + 1)
    with pytest.raises(ValueError, match="byte limit"):
        read_local_listing_master(target)
