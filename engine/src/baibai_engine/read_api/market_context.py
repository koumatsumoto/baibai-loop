"""Web/batch向けMarket Context読取facade。領域自身はこの合成入口をimportしない。"""

from baibai_engine.market_context.macro import (
    MACRO_CONTEXT_STALE_DAYS,
    MACRO_READING_RULES_PATH,
    latest_macro_context_payload,
    list_macro_context_payloads,
    macro_context_payload,
    macro_reading_snapshot,
)
from baibai_engine.market_context.security import build_security_market_context
from baibai_engine.market_context.snapshot import build_market_snapshot

__all__ = [
    "MACRO_CONTEXT_STALE_DAYS",
    "MACRO_READING_RULES_PATH",
    "build_market_snapshot",
    "build_security_market_context",
    "latest_macro_context_payload",
    "list_macro_context_payloads",
    "macro_context_payload",
    "macro_reading_snapshot",
]
