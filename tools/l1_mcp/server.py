"""Research に 3 つの read-only MCP tools で固定 L1 と bounded SQL 結果を見せる。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import StrictInt, ValidationError

from baibai_engine.market.lake.datasets import LAKE_DATASETS
from baibai_engine.market.lake.objects import LakeObjectCache, LakeObjectError, LakeObjectSource
from baibai_engine.market.lake.reader import (
    FixedRelease,
    LakeReadError,
    accepted_dataset,
    resolve_current_release,
    resolve_release,
    selected_partitions,
)

from .contract import LIMITS, L1Error, Limits, Query, ReleaseRef, Scalar, Source, wire
from .gateway import Gateway
from .query import execute_query

ERROR_MESSAGES = {
    "INVALID_ARGUMENT": "入力・列名・期間・parameter を確認してください。",
    "BUSY": "別の読取を実行中です。完了後に再試行してください。",
    "RELEASE_UNAVAILABLE": "指定 release の必要 object を取得できません。世代の補完はしません。",
    "CONTRACT_MISMATCH": "L1 dataset と reader の契約が一致しません。",
    "INTEGRITY_ERROR": "L1 の identity・digest・schema・行数を検証できません。",
    "TRANSFER_BUDGET_EXCEEDED": "gateway 転送上限に達しました。取得範囲を見直してください。",
    "QUERY_LIMIT_EXCEEDED": "query の入力・メモリ上限を超えました。期間・列を減らしてください。",
    "QUERY_TIMEOUT": "SQL の時間上限に達しました。子プロセスは停止済みです。",
    "QUERY_REJECTED": "SQL を実行できません。単一 SELECT / WITH と宣言 source を確認してください。",
    "RESULT_TOO_LARGE": "結果が上限を超えました。集約または期間縮小が必要です。",
    "UNSUPPORTED_RESULT_TYPE": "JSON で扱えない型または非有限値です。SQL で明示変換してください。",
    "UPSTREAM_UNAVAILABLE": "gateway を利用できません。接続・認証を確認してください。",
}


def public_error(exc: Exception) -> L1Error:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, L1Error):
            return current
        current = current.__cause__
    if isinstance(exc, (ValidationError, KeyError, ValueError)):
        return L1Error("INVALID_ARGUMENT")
    if isinstance(exc, LakeReadError):
        if "accepts only" in str(exc) or "partition layout" in str(exc):
            return L1Error("CONTRACT_MISMATCH")
        return L1Error("INTEGRITY_ERROR")
    if isinstance(exc, LakeObjectError):
        return L1Error("INTEGRITY_ERROR")
    return L1Error("UPSTREAM_UNAVAILABLE")


class Adapter:
    def __init__(
        self,
        token: str | None = None,
        *,
        source: LakeObjectSource | None = None,
        limits: Limits = LIMITS,
    ) -> None:
        self.limits = limits
        self.temporary = TemporaryDirectory(prefix="baibai-l1-mcp-")
        self.root = Path(self.temporary.name)
        self.source = source or Gateway(token or "", self.root / "manifests", limits)
        self.cache = LakeObjectCache(self.root / "objects", self.source)
        self.cache.root.mkdir()
        self.lock = threading.Lock()

    def close(self) -> None:
        if isinstance(self.source, Gateway):
            self.source.close()
        self.temporary.cleanup()

    def fixed(self, reference: ReleaseRef) -> FixedRelease:
        return resolve_release(
            self.source,
            reference.release_id,
            manifest_sha256=reference.manifest_sha256,
        )

    def resolve(self) -> dict[str, Any]:
        release = resolve_current_release(self.source)
        return {
            "release_ref": {
                "release_id": release.release_id,
                "manifest_sha256": release.manifest_sha256,
            },
            "created_at": release.manifest.created_at.isoformat(),
            "data_as_of": release.data_as_of.isoformat(),
            "datasets": {
                name: {
                    "data_as_of": item.data_as_of.isoformat(),
                    "coverage_status": item.coverage_status,
                    "rows": item.totals.rows,
                }
                for name, item in release.dataset_manifests.items()
            },
            "limits": asdict(self.limits),
        }

    def describe(self, reference: ReleaseRef, name: str) -> dict[str, Any]:
        if name not in LAKE_DATASETS:
            raise L1Error("INVALID_ARGUMENT")
        release = self.fixed(reference)
        if name not in release.dataset_manifests:
            raise L1Error("INVALID_ARGUMENT")
        dataset = accepted_dataset(release, name)
        manifest = release.dataset_manifest(name)
        return {
            "release_ref": reference.model_dump(),
            "dataset": name,
            "columns": [
                {"name": column.name, "type": str(column.arrow_type), "nullable": column.nullable}
                for column in dataset.columns
            ],
            "primary_key": list(dataset.primary_key),
            "date_column": dataset.date_column,
            "partition_grain": dataset.partition_grain,
            "coverage_start": manifest.coverage_start.isoformat(),
            "data_as_of": manifest.data_as_of.isoformat(),
            "coverage_status": manifest.coverage_status,
            "partitions": [
                {
                    **dict(part.values),
                    "object_count": len(part.objects),
                    "bytes": sum(obj.bytes for obj in part.objects),
                    "rows": sum(obj.rows for obj in part.objects),
                }
                for part in selected_partitions(release, name)
            ],
        }

    def query(self, query: Query) -> dict[str, Any]:
        if any(source.dataset not in LAKE_DATASETS for source in query.sources):
            raise L1Error("INVALID_ARGUMENT")
        release = self.fixed(query.release_ref)
        if any(source.dataset not in release.dataset_manifests for source in query.sources):
            raise L1Error("INVALID_ARGUMENT")
        return execute_query(release, query, self.cache, self.limits)

    def call(self, action: Callable[[], dict[str, Any]]) -> CallToolResult:
        if not self.lock.acquire(blocking=False):
            return self.error(L1Error("BUSY"))
        started = time.monotonic()
        before_gets, before_bytes = self.counters()
        try:
            result = action()
            gets, transferred = self.counters()
            result["transfer"] = {
                "gateway_gets": gets - before_gets,
                "download_bytes": transferred - before_bytes,
                "process_gateway_gets": gets,
                "process_download_bytes": transferred,
            }
            result["elapsed_seconds"] = round(time.monotonic() - started, 6)
            response = CallToolResult(
                content=[
                    TextContent(
                        type="text", text="成功。固定 release と coverage を確認してください。"
                    )
                ],
                structured_content=result,
            )
            # Cap the entire MCP result, including metadata and textual content.
            if (
                len(wire(response.model_dump(mode="json", by_alias=True, exclude_none=True)))
                > self.limits.result_bytes
            ):
                raise L1Error("RESULT_TOO_LARGE")
            return response
        except Exception as exc:
            return self.error(public_error(exc))
        finally:
            self.lock.release()

    def counters(self) -> tuple[int, int]:
        if isinstance(self.source, Gateway):
            return self.source.gets, self.source.transferred_bytes
        return (0, 0)

    @staticmethod
    def error(exc: L1Error) -> CallToolResult:
        message = ERROR_MESSAGES[exc.code]
        return CallToolResult(
            is_error=True,
            content=[TextContent(type="text", text=f"{exc.code}: {message}")],
            structured_content={"error": {"code": exc.code, "message": message}},
        )


def create_server(adapter: Adapter) -> MCPServer[Any]:
    server: MCPServer[Any] = MCPServer(
        "baibai-loop-l1",
        instructions=(
            "最初に l1_resolve_current、次に l1_describe_dataset で schema を確認し、"
            "同じ release_ref を全ての後続 tool に渡してください。"
            "l1_query は日付範囲と source を明示する単一 SELECT / WITH です。"
            "行数超過は失敗です。SQL で集約し、coverage と鮮度を説明してください。"
        ),
    )
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=annotations)
    def l1_resolve_current() -> CallToolResult:
        """current を一度だけ解決し、固定 release_ref・dataset 水準・資源上限を返す。"""
        return adapter.call(adapter.resolve)

    @server.tool(annotations=annotations)
    def l1_describe_dataset(release_ref: ReleaseRef, dataset: str) -> CallToolResult:
        """固定 release の列型・nullable・PK・日付列・partition・coverage を返す。"""
        return adapter.call(lambda: adapter.describe(release_ref, dataset))

    @server.tool(annotations=annotations)
    def l1_query(
        release_ref: ReleaseRef,
        sources: list[Source],
        sql: str,
        parameters: dict[str, Scalar] | None = None,
        max_rows: StrictInt = 1000,
    ) -> CallToolResult:
        """宣言 source を期間・列で限定し、SELECT の schema と rows 配列を返す。

        JOIN / CTE / aggregate / window を利用可。parameters は named scalar。
        最大 2000 行。超過は部分成功にしない。date/time は ISO、decimal は文字列、
        binary は base64。NULL は null、非有限値と nested 型は拒否する。
        """
        return adapter.call(
            lambda: adapter.query(
                Query(
                    release_ref=release_ref,
                    sources=sources,
                    sql=sql,
                    parameters=parameters or {},
                    max_rows=max_rows,
                )
            )
        )

    return server
