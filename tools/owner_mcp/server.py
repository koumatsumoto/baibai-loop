"""候補調査へL1・canonical Triage・時点除外を7つのread-only toolsで見せる。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import ValidationError

from tools.l1_mcp.contract import LIMITS, wire
from tools.l1_mcp.server import Adapter, register_tools

from .reader import OwnerError, Reader, Selector, TriageRef

MESSAGES = {
    "INVALID_ARGUMENT": "selector・固定reference・timezone付き時刻を確認してください。",
    "SOURCE_UNAVAILABLE": "指定sourceまたはbound judgmentを利用できません。補完はしません。",
    "REFERENCE_MISMATCH": "固定referenceと現在のpayloadが一致しません。比較を停止してください。",
    "CONTRACT_MISMATCH": "canonical sourceのdomain契約が整合しません。",
    "AMBIGUOUS_SELECTION": "同日に複数の完全なTriageがあります。exact IDを指定してください。",
    "PORTFOLIO_UNAVAILABLE": "canonical ledgerが未登録のため除外集合を返せません。",
    "PORTFOLIO_COVERAGE_INSUFFICIENT": "ledgerのbroker factは指定時刻までcoverageしていません。",
    "PORTFOLIO_UNRESOLVED": "指定時刻に期限切れ予約の報告が未解決です。",
    "RESULT_TOO_LARGE": "完全な結果がMCP上限を超えました。部分結果は返しません。",
}


def call(action: Callable[[], dict[str, Any]]) -> CallToolResult:
    try:
        result = CallToolResult(
            content=[
                TextContent(type="text", text="成功。固定referenceと入力basisを確認してください。")
            ],
            structured_content=action(),
        )
        if (
            len(wire(result.model_dump(mode="json", by_alias=True, exclude_none=True)))
            > LIMITS.result_bytes
        ):
            raise OwnerError("RESULT_TOO_LARGE")
        return result
    except Exception as exc:
        code = exc.code if isinstance(exc, OwnerError) else "CONTRACT_MISMATCH"
        message = MESSAGES[code]
        return CallToolResult(
            is_error=True,
            content=[TextContent(type="text", text=f"{code}: {message}")],
            structured_content={"error": {"code": code, "message": message}},
        )


def create_server(adapter: Adapter, reader: Reader | None = None) -> MCPServer[Any]:
    reader = reader or Reader()
    server: MCPServer[Any] = MCPServer(
        "baibai-loop-owner",
        instructions=(
            "L1はresolve→describe→queryの順で同じrelease_refを使ってください。"
            "Triageはresolve後のtriage_refを別Chatでも保持してください。"
            "get_inputは実行時production contractの再構築です。"
            "historical exact promptではありません。"
            "選択前に元判断を混ぜない比較ではget_judgmentを呼ばないでください。"
            "portfolioのatはtimezone付き時刻です。store同期・更新は行いません。"
        ),
    )
    register_tools(server, adapter)
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=annotations)
    def triage_resolve(
        research_triage_id: str | None = None,
        as_of: str | None = None,
        not_before: str | None = None,
    ) -> CallToolResult:
        """0〜1 selectorでcomplete Triageを固定。日付はexact、not_beforeは最初、無指定はlatest。"""

        def resolve() -> dict[str, Any]:
            try:
                selector = Selector(
                    research_triage_id=research_triage_id, as_of=as_of, not_before=not_before
                )
            except ValidationError as exc:
                raise OwnerError("INVALID_ARGUMENT") from exc
            return reader.resolve(selector)

        return call(resolve)

    @server.tool(annotations=annotations)
    def triage_get_input(triage_ref: TriageRef) -> CallToolResult:
        """exact sourceからproduction入力を再構築し両hashを検証する。元判断は含まない。"""
        return call(lambda: reader.get_input(triage_ref))

    @server.tool(annotations=annotations)
    def triage_get_judgment(triage_ref: TriageRef) -> CallToolResult:
        """hash検証済みfull canonical ResearchTriageを返す。input policy driftには依存しない。"""
        return call(lambda: reader.get_judgment(triage_ref))

    @server.tool(annotations=annotations)
    def portfolio_get_exclusions(at: str) -> CallToolResult:
        """timezone付き時刻までledgerをreplayし、保有・active予約tickerだけを返す。coverage不足は失敗。"""
        return call(lambda: reader.exclusions(at))

    return server
