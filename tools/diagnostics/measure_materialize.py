"""Issue #1580専用の有限CI計測。fixture生成とexportは別processで呼ぶ。"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import resource
import shutil
import signal
import sqlite3
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from datetime import UTC, date, datetime, timedelta, tzinfo
from functools import wraps
from pathlib import Path
from types import FrameType
from typing import Any, NoReturn, Self
from unittest.mock import patch

from tests.helpers.master import TSE_33_SECTORS, insert_master_rows
from tests.helpers.screening_run import screening_run_payload, stock_analysis
from tests.helpers.screening_sqlite import add_source_coverage

from baibai_engine.collection.economic.store import initialize_database as initialize_indicators
from baibai_engine.market_context import security
from baibai_engine.market_context.reading.rules import (
    DEFAULT_RULES_PATH as READING_RULES,
)
from baibai_engine.screening.rule_config import (
    DEFAULT_RULES_PATH as SCREENING_RULES,
)
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.run_store import ScreeningRunStore
from baibai_engine.storage.application.write import initialize_database
from baibai_engine.storage.observations import schema
from baibai_web import materialize
from baibai_web.sources import db_sources

NOW = datetime(2026, 10, 20, 9, tzinfo=UTC)
CASES = {
    "small-concentrated": (8, 1),
    "medium-concentrated": (256, 1),
    "medium-dispersed": (256, 33),
    "bounded-concentrated": (1000, 1),
    "bounded-dispersed": (1000, 33),
    "upper-concentrated": (4000, 1),
    "upper-dispersed": (4000, 33),
}


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls.fromtimestamp(NOW.timestamp(), tz)  # CIのTZはUTC。


@contextmanager
def fixed_clock() -> Iterator[None]:
    with ExitStack() as stack:
        for name in (
            "baibai_web.materialize",
            "baibai_web.sources.db_sources",
            "baibai_web.sources.calibration_context",
            "baibai_web.readmodel.stocks.detail",
            "baibai_web.readmodel.stocks.screening",
            "baibai_web.readmodel.builders.dashboard",
            "baibai_web.readmodel.builders.tasks",
            "baibai_web.readmodel.builders.meta",
            "baibai_web.readmodel.builders.daily_delta",
        ):
            stack.enter_context(
                patch.object(importlib.import_module(name), "datetime", FixedDatetime)
            )
        yield


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def trading_days() -> list[date]:
    days: list[date] = []
    day = NOW.date()
    while len(days) < 750:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return list(reversed(days))


def create_fixture(root: Path, case: str) -> dict[str, object]:
    count, sectors = CASES[case]
    root.mkdir(parents=True)  # 既存storeを上書きしない。
    for relative in (Path("web/config/market-context-panel.yaml"), READING_RULES, SCREENING_RULES):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(relative, target)
    initialize_database(root / "stores/application/baibai.sqlite")
    initialize_indicators(root / "stores/market_context/indicators.sqlite").close()
    days, names = trading_days(), sorted(TSE_33_SECTORS)
    with closing(schema.open_connection(root / "stores/market/market.sqlite")) as conn, conn:
        insert_master_rows(
            conn,
            [
                (
                    str(days[0]),
                    str(2000 + i),
                    f"Synthetic {i}",
                    "プライム",
                    names[i % sectors],
                    "011",
                )
                for i in range(count)
            ],
        )
        conn.executemany(
            "INSERT INTO jquants_daily_bars "
            "(ticker, traded_at, close, adjustment_factor, turnover_value) "
            "VALUES (?, ?, ?, 1, 200000000)",
            (
                (str(2000 + i), str(day), 1000 + 10 * (i % 17) + offset * (1 + i % 5))
                for i in range(count)
                for offset, day in enumerate(days)
                if not (i == 1 and offset == 729 or i == 2 and offset == 749)
            ),
        )
        conn.executemany(
            "INSERT INTO jquants_daily_bars (ticker, traded_at, close, adjustment_factor) "
            "VALUES ('1321', ?, ?, 1)",
            [(str(day), 2000 + offset) for offset, day in enumerate(days)],
        )
        conn.executemany(
            "INSERT INTO external_assessment_model_valuation_observations "
            "(ticker, fetched_at_utc, snapshot_date, distributor, source_url, parser_version, "
            "contract_version, consensus_target, currency, unit, share_basis, "
            "consensus_target_origin, consensus_target_period) "
            "VALUES (?, ?, ?, 'synthetic', 'https://example.invalid/fixture', 'fixture-1', "
            "'1', ?, 'JPY', 'yen_per_share', 'fixture-basis', 'synthetic', '2027-03')",
            [
                (str(2000 + i), f"{day}T08:00:00.000000+00:00", day, price)
                for i in range(count)
                for day, price in (("2026-10-19", 3000), ("2026-10-20", 3100))
            ],
        )
        add_source_coverage(
            conn,
            source="jquants_daily_bars",
            coverage_key="synthetic",
            record_count=(count + 1) * 750 - 2,
            min_date=str(days[0]),
            max_date=str(days[-1]),
            fetched_at_utc=NOW.isoformat(),
        )
    payload = screening_run_payload(
        as_of=str(NOW.date()),
        universe_size=count,
        stock_analyses=[
            stock_analysis(str(2000 + i), name=f"Synthetic {i}", sector_33=names[i % sectors])
            for i in range(count)
        ],
    )
    with patch("baibai_engine.screening.run_store.store.datetime", FixedDatetime):
        ScreeningRunStore(root / "stores/screening/runs.sqlite").publish_run(
            payload,
            rules=load_screening_rules(),
            run_revision_id="run-revision-00000000000040008000000000000001",
        )
    return {
        "recipe": "materialize-1580-v1",
        "case": case,
        "securities": count,
        "sectors": sectors,
        "bars": 750,
        "price_rows": (count + 1) * 750 - 2,
        "external_rows": 2 * count,
        "fixed_now": NOW.isoformat(),
        "inputs_sha256": {
            p.relative_to(root).as_posix(): file_sha256(p)
            for p in sorted(root.rglob("*"))
            if p.is_file()
        },
        "input_bytes": sum(p.stat().st_size for p in root.rglob("*") if p.is_file()),
    }


def output_inventory(output: Path) -> dict[str, object]:
    raw, semantic, total_bytes = hashlib.sha256(), hashlib.sha256(), 0
    groups: Counter[str] = Counter()
    for path in sorted(output.rglob("*.json")):
        name, content = path.relative_to(output).as_posix(), path.read_bytes()
        canonical = json.dumps(
            json.loads(content), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        for digest, data in ((raw, content), (semantic, canonical)):
            digest.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
        total_bytes += len(content)
        groups["security" if name.startswith("views/security--") else name.split("/")[0]] += 1
    return {
        "files": sum(groups.values()),
        "bytes": total_bytes,
        "file_groups": dict(groups),
        "raw_tree_sha256": raw.hexdigest(),
        "semantic_json_tree_sha256": semantic.hexdigest(),
    }


def check_oracle(output: Path, case: str) -> None:
    """Productionの算術helperを使わず、先頭3銘柄の端点・medianを検算する。"""
    count, sectors = CASES[case]
    days = trading_days()
    for i, start, end, bars in ((0, 729, 749, 750), (1, 728, 749, 749), (2, 728, 748, 749)):
        body = json.loads((output / f"views/security--{2000 + i}.json").read_bytes())
        price, relative = body["market_context"]["price"], body["market_context"]["relative"]
        base, slope = 1000 + 10 * (i % 17), 1 + i % 5
        own = (base + end * slope) / (base + start * slope) - 1
        assert price["bar_count"] == bars
        assert price["resolved_date"] == str(days[end])
        assert price["close"] == base + end * slope
        assert math.isclose(price["return_20d"], own, abs_tol=1e-12)
        returns = sorted(
            (
                (1000 + 10 * (peer % 17) + end * (1 + peer % 5))
                / (1000 + 10 * (peer % 17) + start * (1 + peer % 5))
                - 1
            )
            for peer in range(count)
            if peer != i
            and peer % sectors == i % sectors
            and not (peer == 1 and 729 in (start, end) or peer == 2 and 749 in (start, end))
        )
        sector = relative["sector"]
        if returns:
            median = (returns[(len(returns) - 1) // 2] + returns[len(returns) // 2]) / 2
            assert sector["peer_count"] == len(returns)
            assert sector["comparison_start_date"] == str(days[start])
            assert sector["comparison_end_date"] == str(days[end])
            assert math.isclose(sector["peer_median_return_20d"], median, abs_tol=1e-12)
            assert math.isclose(sector["relative_return_20d"], own - median, abs_tol=1e-12)
        else:
            assert sector is None
        inputs = body["external_assessment"]["model_valuations"][0]["inputs"]
        assert inputs["latest"]["consensus_target"] == 3100
        assert inputs["previous"]["consensus_target"] == 3000


class Probe:
    """単一threadのinclusive wall/CPUを集計する一時的なobserver。"""

    def __init__(self) -> None:
        self.stages: dict[str, dict[str, int]] = {}
        self.stack: list[str] = []
        self.connections: Counter[str] = Counter()
        self.schema_checks: Counter[str] = Counter()
        self.sector_endpoint_calls = self.sector_medians = 0

    def wrap(self, label: str, function: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(function)
        def observed(*args: Any, **kwargs: Any) -> Any:
            caller = self.stack[-1] if self.stack else "outside"
            if label == "sqlite.connect":
                name = str(args[0]).split("?")[0].rsplit("/", 1)[-1]
                self.connections[f"{caller}:{name}"] += 1
            if label == "market.schema_validation":
                self.schema_checks[caller] += 1
            if label == "statistics.median" and caller == "security.sector":
                self.sector_medians += 1
            wall, cpu = time.perf_counter_ns(), time.process_time_ns()
            self.stack.append(label)
            try:
                return function(*args, **kwargs)
            finally:
                elapsed, used = time.perf_counter_ns() - wall, time.process_time_ns() - cpu
                self.stack.pop()
                row = self.stages.setdefault(label, {"calls": 0, "wall_ns": 0, "cpu_ns": 0})
                row["calls"] += 1
                row["wall_ns"] += elapsed
                row["cpu_ns"] += used

        return observed

    @contextmanager
    def install(self) -> Iterator[None]:
        with ExitStack() as stack:
            for name in (
                "build_sources",
                "validate_reading_rules",
                "validate_market_store_hydration",
                "build_dashboard",
                "build_tasks",
                "prepare_security_inputs",
                "build_screening",
                "build_daily_delta",
                "build_market_context",
                "build_indicator_series",
                "build_security_detail",
                "_write_history",
                "build_meta",
                "_write_model",
            ):
                stack.enter_context(
                    patch.object(
                        materialize, name, self.wrap(f"export.{name}", getattr(materialize, name))
                    )
                )
            for owner, name, label in (
                (db_sources.DbMarketPriceSource, "prepare_market_context", "market.prepare"),
                (
                    db_sources.DbExternalAssessmentSource,
                    "assessment",
                    "security.external_assessment",
                ),
                (
                    db_sources,
                    "build_security_market_contexts"
                    if hasattr(db_sources, "build_security_market_contexts")
                    else "build_security_market_context",
                    "security.context",
                ),
                (security, "read_price_bars", "security.prices"),
                (security, "_price_block", "security.price_block"),
                (security, "_relative_block", "security.relative"),
                (security, "_sector_block", "security.sector"),
                (schema, "_validate_current_schema", "market.schema_validation"),
                (sqlite3, "connect", "sqlite.connect"),
                (security.statistics, "median", "statistics.median"),
            ):
                stack.enter_context(
                    patch.object(owner, name, self.wrap(label, getattr(owner, name)))
                )
            endpoint = security._endpoint_return

            def counted_endpoint(*args: Any, **kwargs: Any) -> float | None:
                if self.stack and self.stack[-1] == "security.sector":
                    self.sector_endpoint_calls += 1
                return endpoint(*args, **kwargs)

            stack.enter_context(patch.object(security, "_endpoint_return", counted_endpoint))
            yield

    def report(self) -> dict[str, object]:
        return {
            "stages": self.stages,
            "sqlite_connections_by_caller": dict(self.connections),
            "market_schema_validations_by_caller": dict(self.schema_checks),
            "sector_medians": self.sector_medians,
            "sector_endpoint_calls_including_own": self.sector_endpoint_calls,
            "peer_return_attempts": self.sector_endpoint_calls - self.sector_medians,
        }


def deny_external_io(event: str, args: tuple[object, ...]) -> None:
    if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system"}:
        raise RuntimeError(f"synthetic CI measurement forbids external I/O: {event}")


def _timeout(signum: int, frame: FrameType | None) -> NoReturn:
    raise TimeoutError("finite CI measurement budget exhausted (SIGTERM)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("fixture", "measure"))
    parser.add_argument("--case", required=True, choices=tuple(CASES))
    parser.add_argument("--work-dir", required=True, type=Path)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        parser.error("this temporary diagnostic may run only in CI")
    sys.addaudithook(deny_external_io)
    signal.signal(signal.SIGTERM, _timeout)
    work = args.work_dir.resolve()
    root, report_path = work / "fixture", work / "profile.json"
    report: dict[str, Any]
    if args.phase == "fixture":
        work.mkdir(parents=True, exist_ok=False)
        wall, cpu = time.perf_counter_ns(), time.process_time_ns()
        fixture = create_fixture(root, args.case)
        report = {
            "status": "fixture_ready",
            "source_commit": os.environ["GITHUB_SHA"],
            "uv_lock_sha256": file_sha256(Path("uv.lock")),
            "python": sys.version,
            "sqlite": sqlite3.sqlite_version,
            "runner_image": os.environ.get("ImageVersion"),
            "available_cpus": len(os.sched_getaffinity(0)),
            "fixture": fixture,
            "fixture_wall_ns": time.perf_counter_ns() - wall,
            "fixture_cpu_ns": time.process_time_ns() - cpu,
            "fixture_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        }
    else:
        report = json.loads(report_path.read_bytes())
        assert report["fixture"]["case"] == args.case
        for relative, expected in report["fixture"]["inputs_sha256"].items():
            assert file_sha256(root / relative) == expected, relative
        probe, output = Probe(), work / "serving"
        report["status"] = "failed"
        try:
            with fixed_clock(), probe.install():
                written = probe.wrap("export.total", materialize.export_read_models)(root, output)
            report["peak_rss_after_export_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            report["output"] = output_inventory(output)
            assert len(written) == report["output"]["files"]
            assert report["output"]["file_groups"]["security"] == CASES[args.case][0]
            check_oracle(output, args.case)
            report["oracle"] = "passed"
            if args.case == "small-concentrated":
                with fixed_clock():
                    materialize.export_read_models(root, work / "plain-serving")
                assert output_inventory(work / "plain-serving") == report["output"]
                report["observer_output_parity"] = "passed"
            report["status"] = "measured"
        except Exception as error:
            report["error"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            report.setdefault(
                "peak_rss_after_export_kib", resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            )
            report["profile"] = probe.report()
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
