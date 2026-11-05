"""Freeze native master references for acquisition consumers, without a market release."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

from baibai_engine.data_collection.providers.jquants.dependencies import (
    dependency_covers_day,
    dependency_from_request,
)
from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP
from baibai_engine.data_collection.providers.jquants.master_native import (
    MasterL1,
    MasterObjectStore,
    read_master_requests,
)
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.master_inputs import read_master_input
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.stocks.master_snapshot import (
    MASTER_SOURCE,
    master_coverage_key,
)
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store, ObjectStore
from baibai_engine.storage.paths import MARKET_DB_PATH
from baibai_jobs.acquisition.calendar_dependencies import (
    CalendarDependencyUnavailable,
    prepare_tradingview_calendar,
)
from baibai_jobs.acquisition.jquants_master_native import acquire_master_native
from baibai_jobs.calendar_cutover import calendar_native_enabled
from baibai_jobs.integration.master_native import fixed_input, materialize_saved_input
from baibai_jobs.maintenance.tradingview_context import calendar_context
from baibai_jobs.master_cutover import master_native_enabled


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
) -> dict[str, Any]:
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
        if (
            request is None
            or item.error is not None
            or request.source != "master"
            or not dependency_covers_day("master", request.params, day)
        ):
            raise ValueError("fixed native master differs from its required date")
        return dependency_from_request(item.request_id, request)
    raise ValueError("fixed native master receipt is unavailable")


def _clear_master_projection(market: Path, day: date) -> None:
    # This is only an acquisition execution copy. Never let stale L2 satisfy
    # preflight when native state is missing, failed, held, empty or invalid.
    with sqlite3.connect(market) as connection:
        connection.execute(
            "DELETE FROM jquants_master_snapshots WHERE snapshot_date=?", (day.isoformat(),)
        )
        connection.execute(
            "DELETE FROM source_coverage WHERE source=? AND coverage_key=?",
            (MASTER_SOURCE, master_coverage_key(day)),
        )


def prepare_tradingview_master(
    store: MasterObjectStore,
    *,
    directory: Path,
    market: Path,
    observation_date: date,
    acquire: bool = False,
    client: JQuantsHTTP | None = None,
) -> bool:
    """Preserve TradingView's calendar basis and its existing early fetch opportunity."""
    if calendar_native_enabled() and not prepare_tradingview_calendar(
        store, directory=directory, market=market, observation_date=observation_date
    ):
        raise CalendarDependencyUnavailable("native TradingView calendar is unavailable")
    with closing(sqlite3.connect(market)) as connection:
        basis, _business = calendar_context(connection, observation_date)
    report = json.loads((directory / "result.json").read_bytes())
    report.pop("master_native_input", None)
    try:
        if acquire:
            if client is None:
                raise ValueError("native master acquisition requires its HTTP client")
            outcome = acquire_master_native(
                MasterL1(store, directory / ("master-native-acquire-" + uuid.uuid4().hex)),
                start=basis,
                end=basis,
                client=client,
                force=True,
            )
            if outcome["failed"] or outcome["held"] or outcome["incomplete"]:
                raise MasterDependencyUnavailable("native master acquisition remains unresolved")
        saved = fix_master_dependency(store, directory=directory / "master-native", day=basis)
        materialize_saved_input(store, saved, market=market, days=[basis])
        report["master_native_input"] = saved.model_dump(mode="json")
    except (MasterDependencyUnavailable, JQuantsProviderError):
        _clear_master_projection(market, basis)
        write_json_atomic(directory / "result.json", report)
        if acquire:
            raise
        return False
    write_json_atomic(directory / "result.json", report)
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("hydrate", "acquire"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--asof", type=date.fromisoformat, required=True)
    parser.add_argument("--market", type=Path, default=MARKET_DB_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not master_native_enabled():
        return 0
    try:
        with closing(
            Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        ) as store:
            if args.command == "acquire":
                with closing(JQuantsHTTP(os.environ.get("JQUANTS_API_KEY", ""))) as client:
                    prepare_tradingview_master(
                        store,
                        directory=args.directory,
                        market=args.market,
                        observation_date=args.asof,
                        acquire=True,
                        client=client,
                    )
            else:
                prepare_tradingview_master(
                    store,
                    directory=args.directory,
                    market=args.market,
                    observation_date=args.asof,
                )
    except Exception as error:
        print(f"native master dependency failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
