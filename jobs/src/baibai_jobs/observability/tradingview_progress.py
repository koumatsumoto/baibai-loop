"""TradingView進捗の公開field境界。依存導入失敗時もstdlibだけで読める。"""

from __future__ import annotations

# Writer・例外表示・workflow logが同じallowlistとscalar条件を使う。
PROGRESS_FIELDS = (
    "snapshot_date",
    "master_asof",
    "phase",
    "expected_universe",
    "chunks_total",
    "chunks_completed",
    "requests_attempted",
    "symbols_requested",
    "chunk_index",
    "chunk_size",
    "columns_count",
    "interval_seconds",
    "response_bytes",
    "provider_elapsed_seconds",
    "max_chunk_elapsed_seconds",
    "elapsed_seconds",
    "first_fetched_at_utc",
    "last_fetched_at_utc",
    "oauth_rotations",
)


def safe_progress(payload: dict[str, object]) -> dict[str, object]:
    return {
        key: payload[key]
        for key in PROGRESS_FIELDS
        if key in payload
        and (payload[key] is None or type(payload[key]) in (str, int, float, bool))
    }

