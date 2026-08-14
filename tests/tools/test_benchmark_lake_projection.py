from __future__ import annotations

from tools.diagnostics.benchmark_lake_projection import (
    MIN_BASELINE_ROWS,
    _acceptance,
    _implementation_sha256,
)


def _passing_payload() -> dict[str, object]:
    return {
        "cold_seconds": 1_000.0,
        "reuse_seconds": 100.0,
        "output_bytes": 2 * 1024**3,
        "peak_rss_bytes": 1024**3,
        "minimum_free_disk_bytes": 10 * 1024**3,
        "rows": dict(MIN_BASELINE_ROWS),
        "queries": {
            "point": {"p95_ms": 10.0},
            "range": {"p95_ms": 20.0},
        },
        "sqlite_stat_rows": 2,
    }


def test_scale_acceptance_requires_every_budget() -> None:
    acceptance = _acceptance(_passing_payload())

    assert acceptance
    assert all(acceptance.values())


def test_three_year_projection_and_query_latency_fail_closed() -> None:
    payload = _passing_payload()
    payload["cold_seconds"] = 1_300.0
    payload["queries"] = {"point": {"p95_ms": 101.0}}

    acceptance = _acceptance(payload)

    assert acceptance["cold_build"] is True
    assert acceptance["three_year_cold"] is False
    assert acceptance["query_latency"] is False


def test_implementation_identity_is_a_sha256_of_the_current_generator_and_lake_code() -> None:
    identity = _implementation_sha256()

    assert len(identity) == 64
    assert set(identity) <= set("0123456789abcdef")
