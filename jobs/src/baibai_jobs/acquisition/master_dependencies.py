"""Freeze native master references for acquisition consumers, without a market release."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsRequest
from baibai_engine.data_collection.providers.jquants.master_native import (
    MasterL1,
    MasterObjectStore,
    is_master_for_day,
    read_master_requests,
)
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.master_inputs import read_master_input
from baibai_engine.storage.objects.r2 import ObjectStore
from baibai_jobs.integration.master_native import fixed_input


class MasterDependencyUnavailable(ValueError):
    """No fresh, unambiguous adopted receipt exists for the explicitly required date."""


def fix_master_dependency(store: ObjectStore, *, directory: Path, day: date) -> InputSnapshot:
    fixed = MasterL1(cast(MasterObjectStore, store), directory).fixed()
    scope = fixed.value()["scopes"].get(day.isoformat(), {})
    if not scope.get("adopted") or scope.get("failure") or scope.get("held"):
        raise MasterDependencyUnavailable("exact native master acquisition is unresolved")
    return fixed_input(store, fixed, as_of=day, evaluated_at=datetime.now(UTC), days=[day])


def saved_master_dependency(
    store: ObjectStore, saved: InputSnapshot, *, day: date
) -> JQuantsRequest:
    """Resolve only the immutable saved membership; never consult the latest head."""
    saved = InputSnapshot.model_validate_json(saved.model_dump_json())
    if saved.kind != "native_l1" or saved.descriptor is None:
        raise ValueError("native master dependency requires a fixed input")
    selected = read_master_input(store, saved.descriptor, days=[day])
    scope = selected["scopes"].get(day.isoformat(), {})
    if not scope.get("adopted") or scope.get("failure") or scope.get("held"):
        raise MasterDependencyUnavailable("saved exact native master acquisition is unresolved")
    for item in read_master_requests(store, **selected, days=[day]):
        if item.request_id != scope["adopted"]:
            continue
        request = item.request
        if request is None or item.error is not None or not is_master_for_day(request, day):
            raise ValueError("fixed native master differs from its required date")
        return request
    raise ValueError("fixed native master receipt is unavailable")
