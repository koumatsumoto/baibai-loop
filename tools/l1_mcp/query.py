"""Research の宣言済み L1 source を隔離 SQL で測り、上限超過の結果を止める。"""

from __future__ import annotations

import base64
import json
import math
import re
import resource
import subprocess  # nosec B404
import sys
import time
from dataclasses import asdict
from datetime import date, datetime
from datetime import time as datetime_time
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import duckdb

from baibai_engine.market.lake.datasets import LakeDataset
from baibai_engine.market.lake.objects import LakeObjectCache
from baibai_engine.market.lake.reader import (
    FixedRelease,
    accepted_dataset,
    partition_objects,
    partition_period,
    selected_partitions,
    verify_object,
)

from .contract import LIMITS, L1Error, Limits, Query, Source, wire


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def plan_sources(release: FixedRelease, query: Query) -> list[dict[str, Any]]:
    plans = []
    for source in query.sources:
        dataset = accepted_dataset(release, source.dataset)
        columns = source.columns or [column.name for column in dataset.columns]
        if not set(columns) <= {column.name for column in dataset.columns}:
            raise L1Error("INVALID_ARGUMENT")
        layout = dataset.partition_by
        low = date.fromisoformat(source.from_date)
        high = date.fromisoformat(source.to_date)
        start = (low.year, low.month) if len(layout) == 2 else (low.year,)
        end = (high.year, high.month) if len(layout) == 2 else (high.year,)
        partitions = tuple(
            part
            for part in selected_partitions(release, source.dataset)
            if start <= partition_period(part, layout) <= end
        )
        manifest = release.dataset_manifest(source.dataset)
        objects = partition_objects(partitions)
        plans.append(
            {
                "dataset": dataset,
                "source": source,
                "objects": objects,
                "summary": {
                    **source.model_dump(by_alias=True),
                    "columns": columns,
                    "coverage_start": manifest.coverage_start.isoformat(),
                    "data_as_of": manifest.data_as_of.isoformat(),
                    "coverage_status": manifest.coverage_status,
                    "selected_partition_count": len(partitions),
                    "selected_bytes": sum(obj.bytes for obj in objects),
                    "manifest_rows": sum(obj.rows for obj in objects),
                },
            }
        )
    return plans


def execute_query(
    release: FixedRelease,
    query: Query,
    cache: LakeObjectCache,
    limits: Limits = LIMITS,
) -> dict[str, Any]:
    plans = plan_sources(release, query)
    # Count the union before the first object download, even when objects are cached.
    objects = {obj.key: obj for plan in plans for obj in plan["objects"]}
    if sum(obj.bytes for obj in objects.values()) > limits.query_bytes:
        raise L1Error("QUERY_LIMIT_EXCEEDED")
    loaded = []
    for plan in plans:
        dataset: LakeDataset = plan["dataset"]
        source: Source = plan["source"]
        paths = []
        for obj in plan["objects"]:
            path = cache.materialize(obj)
            verify_object(path, dataset, obj)
            paths.append(str(path))
        loaded.append(
            {
                "alias": source.alias,
                "paths": paths,
                "columns": plan["summary"]["columns"],
                "date_column": dataset.date_column,
                "from": source.from_date,
                "to": source.to_date,
                "schema": {column.name: column.sqlite_type for column in dataset.columns},
            }
        )
    result = run_child(
        {
            "sources": loaded,
            "sql": query.sql,
            "parameters": query.parameters,
            "max_rows": query.max_rows,
            "limits": asdict(limits),
        },
        cache.root,
        limits,
    )
    return {
        "release_ref": query.release_ref.model_dump(),
        "sources": [plan["summary"] for plan in plans],
        **result,
        "notes": [
            "固定 release の観測値。世代整合性は point-in-time の投資可能情報を保証しません。",
            "coverage_status は保有データの網羅性です。空の期間や集約結果の完全性とは別です。",
            "鮮度は source ごとの data_as_of と requested date range を照合してください。",
        ],
    }


def run_child(payload: dict[str, Any], root: Path, limits: Limits) -> dict[str, Any]:
    # exec starts a fresh interpreter, rather than forking a secret-bearing Python heap.
    repository = Path(__file__).resolve().parents[2]
    bootstrap = (
        "import sys,runpy;sys.path.insert(0,sys.argv[1]);"
        "runpy.run_module('tools.l1_mcp.query',run_name='__main__')"
    )
    with (
        TemporaryDirectory(prefix="query-", dir=root) as working,
        subprocess.Popen(  # nosec B603
            [sys.executable, "-I", "-c", bootstrap, str(repository)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=working,
            env={"LANG": "C.UTF-8", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
        ) as child,
    ):
        try:
            stdout, _ = child.communicate(wire(payload), timeout=limits.sql_seconds)
        except BaseException as exc:
            child.terminate()
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            if isinstance(exc, subprocess.TimeoutExpired):
                raise L1Error("QUERY_TIMEOUT") from None
            raise
        if child.returncode or not stdout:
            raise L1Error("QUERY_LIMIT_EXCEEDED")
    if len(stdout) > limits.result_bytes:
        raise L1Error("RESULT_TOO_LARGE")
    result: dict[str, Any] = json.loads(stdout)
    if "error" in result:
        raise L1Error(result["error"])
    return result


def json_scalar(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise L1Error("UNSUPPORTED_RESULT_TYPE")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise L1Error("UNSUPPORTED_RESULT_TYPE")
        return str(value)
    if isinstance(value, (date, datetime, datetime_time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    raise L1Error("UNSUPPORTED_RESULT_TYPE")


def child_query(payload: dict[str, Any], limits: Limits) -> dict[str, Any]:
    started = time.monotonic()
    resource.setrlimit(resource.RLIMIT_AS, (limits.child_memory_bytes, limits.child_memory_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    with duckdb.connect(
        config={
            "threads": "1",
            "memory_limit": str(limits.duckdb_memory_bytes) + "B",
            "temp_directory": "",
            "allow_persistent_secrets": "false",
            "autoload_known_extensions": "false",
            "autoinstall_known_extensions": "false",
            "allow_community_extensions": "false",
            "allow_unsigned_extensions": "false",
            "python_enable_replacements": "false",
        }
    ) as connection:
        for source in payload["sources"]:
            projection = ", ".join(quote(name) for name in source["columns"])
            if source["paths"]:
                # Identifiers are quoted; files and dates are bound parameters.
                statement = (
                    f"CREATE TABLE {quote(source['alias'])} AS SELECT {projection} "  # nosec B608
                    f"FROM read_parquet($files, union_by_name=false, hive_partitioning=false) "
                    f"WHERE {quote(source['date_column'])} >= $start "
                    f"AND {quote(source['date_column'])} <= $end"
                )
                connection.execute(
                    statement,
                    {
                        "files": source["paths"],
                        "start": source["from"],
                        "end": source["to"],
                    },
                )
            else:
                types = {"TEXT": "VARCHAR", "REAL": "DOUBLE", "INTEGER": "BIGINT"}
                columns = ", ".join(
                    f"{quote(name)} {types[source['schema'][name]]}" for name in source["columns"]
                )
                connection.execute(f"CREATE TABLE {quote(source['alias'])} ({columns})")
        connection.execute("SET enable_external_access=false")
        connection.execute("SET lock_configuration=true")
        sql = payload["sql"]
        tokens = duckdb.tokenize(sql)
        # DuckDB rewrites some PRAGMAs to SELECT; check the lexer entry as well as the AST.
        if not tokens or not re.match(r"(?i)(SELECT|WITH)\b", sql[tokens[0][0] :]):
            raise L1Error("QUERY_REJECTED")
        statements = connection.extract_statements(sql)
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise L1Error("QUERY_REJECTED")
        if statements[0].named_parameters != set(payload["parameters"]):
            raise L1Error("INVALID_ARGUMENT")
        connection.execute(statements[0], payload["parameters"])
        schema = [{"name": col[0], "type": str(col[1])} for col in connection.description]
        rows: list[list[object]] = []
        size = len(wire(schema))
        for _ in range(payload["max_rows"] + 1):
            row = connection.fetchone()
            if row is None:
                break
            if len(rows) == payload["max_rows"]:
                raise L1Error("RESULT_TOO_LARGE")
            converted = [json_scalar(value) for value in row]
            size += len(wire(converted)) + 1
            if size > limits.result_bytes:
                raise L1Error("RESULT_TOO_LARGE")
            rows.append(converted)
    return {
        "schema": schema,
        "rows": rows,
        "row_count": len(rows),
        "execution": {
            "seconds": round(time.monotonic() - started, 6),
            "peak_memory_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        },
    }


def child_main() -> None:
    try:
        payload = json.load(sys.stdin)
        limits = Limits(**payload["limits"])
        try:
            result = child_query(payload, limits)
            if len(wire(result)) > limits.result_bytes:
                raise L1Error("RESULT_TOO_LARGE")
        except L1Error as exc:
            result = {"error": exc.code}
        except (MemoryError, duckdb.OutOfMemoryException):
            result = {"error": "QUERY_LIMIT_EXCEEDED"}
        except Exception:
            result = {"error": "QUERY_REJECTED"}
        sys.stdout.buffer.write(wire(result))
    except Exception:
        sys.stdout.buffer.write(b'{"error":"QUERY_REJECTED"}')


if __name__ == "__main__":
    child_main()
