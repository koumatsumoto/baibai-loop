from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.helpers.screening_run import screening_run_payload, security_analysis
from tests.web.test_screening_judgment_binding import _dependencies, _frozen_entry

from baibai_engine.appdb.json import canonical_json
from baibai_engine.screening.discovery.review_set import build_review_set
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.run_store import ScreeningRunStore
from baibai_web.readmodel.stocks import (
    _operative_run,
    _review_set_entries_entry_view,
    build_screening,
    build_security_detail,
)
from baibai_web.sources.db_sources import DbScreeningSource


def _publish_run(store: ScreeningRunStore, as_of: str, revision: str) -> None:
    store.publish_run(
        screening_run_payload(as_of=as_of, security_analyses=[]),
        run_revision_id=revision,
    )


def _insert_review_set(
    path: Path,
    revision: str,
    as_of: str,
    name: str,
    created: str,
    entries: list[dict[str, object]] | None = None,
) -> None:
    config = load_screening_rules()
    payload = build_review_set(
        [], rules=config.candidate_discovery, required_jpx_flags=config.universe.required_jpx_flags
    )
    payload.update(
        entries=entries or [],
        review_set_id=name,
        run_revision_id=revision,
        as_of=as_of,
        created_at=created,
        screening_rules_hash="rules-fixture",
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO review_set(review_set_id, run_revision_id, created_at, payload) "
            "VALUES (?, ?, ?, ?)",
            (name, revision, created, canonical_json(payload)),
        )


def test_web_fallback_uses_owner_asof_before_later_publication(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(path)
    _publish_run(store, "2026-09-18", "run-old")
    _publish_run(store, "2026-09-19", "run-new")
    _publish_run(store, "2026-09-20", "run-incomplete")
    _insert_review_set(path, "run-new", "2026-09-19", "review-new", "2026-09-19T18:00:00+09:00")
    _insert_review_set(path, "run-old", "2026-09-18", "review-old", "2026-09-21T18:00:00+09:00")

    source = DbScreeningSource(path)
    assert [item["review_set_id"] for item in source.review_sets()] == ["review-new", "review-old"]
    run, views = _operative_run(source)
    assert run is not None
    assert run.run_revision_id == "run-new"
    assert [view.review_set_id for view in views] == ["review-new"]


def test_owner_order_handles_offsets_and_id_tie_and_web_keeps_it(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(path)
    _publish_run(store, "2026-09-19", "run-new")
    _publish_run(store, "2026-09-20", "run-incomplete")
    _insert_review_set(path, "run-new", "2026-09-19", "review-a", "2026-09-19T10:00:00+00:00")
    _insert_review_set(path, "run-new", "2026-09-19", "review-b", "2026-09-19T19:00:00+09:00")
    _insert_review_set(path, "run-new", "2026-09-19", "review-c", "2026-09-19T20:00:00+09:00")
    source = DbScreeningSource(path)

    assert [item["review_set_id"] for item in source.review_sets()] == [
        "review-c",
        "review-b",
        "review-a",
    ]
    run, views = _operative_run(source)
    assert run is not None
    assert run.run_revision_id == "run-new"
    assert [view.review_set_id for view in views] == ["review-c", "review-b", "review-a"]


def test_quality_projection_keeps_nonexact_and_old_snapshot() -> None:
    for quality in ("exact", "approximated", "unavailable", None):
        entry = _frozen_entry()
        entry["analysis"]["data_quality"].update(
            ttm_quality_ev_ebitda=quality, ttm_quality_fcf=quality
        )
        projected = _review_set_entries_entry_view(entry).analysis.data_quality
        assert projected.ttm_quality_ev_ebitda == quality
        assert projected.ttm_quality_fcf == quality

    old = _review_set_entries_entry_view(_frozen_entry()).analysis.data_quality
    assert old.ttm_quality_ev_ebitda is None
    assert old.ttm_quality_fcf is None


def test_latest_run_owns_its_review_set_and_absent_store_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    assert _operative_run(DbScreeningSource(path)) == (None, [])

    store = ScreeningRunStore(path)
    _publish_run(store, "2026-09-19", "run-old")
    _publish_run(store, "2026-09-20", "run-new")
    source = DbScreeningSource(path)
    run, views = _operative_run(source)
    assert run is not None
    assert run.run_revision_id == "run-new"
    assert views == []

    _insert_review_set(path, "run-old", "2026-09-19", "review-old", "2026-09-21T18:00:00+09:00")
    _insert_review_set(path, "run-new", "2026-09-20", "review-new", "2026-09-20T18:00:00+09:00")
    run, views = _operative_run(source)
    assert run is not None
    assert run.run_revision_id == "run-new"
    assert [view.review_set_id for view in views] == ["review-new"]


def test_list_and_detail_bind_to_the_same_fallback_run_and_fv(tmp_path: Path, mocker) -> None:
    path = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(path)
    store.publish_run(
        screening_run_payload(
            as_of="2026-09-19",
            security_analyses=[security_analysis("2331", metrics={"market_price_yen": 1000.0})],
        ),
        run_revision_id="run-complete",
    )
    _publish_run(store, "2026-09-20", "run-incomplete")
    entry = _frozen_entry()
    entry["nominations"][0]["rank"] = 1
    _insert_review_set(
        path,
        "run-complete",
        "2026-09-19",
        "review-complete",
        "2026-09-19T18:00:00+09:00",
        entries=[entry],
    )
    source = DbScreeningSource(path)
    ledger, research = _dependencies(mocker)
    research.position_reviews.return_value = []
    market = mocker.Mock()

    listing = build_screening(source, ledger, research)
    detail = build_security_detail("2331", ledger, research, source, market)

    assert detail is not None
    assert detail.security_analysis is not None
    assert listing.run is not None
    assert detail.screening_run is not None
    assert listing.run.run_revision_id == detail.screening_run.run_revision_id == "run-complete"
    assert listing.review_sets[0].review_set_id == "review-complete"
    assert listing.security_analyses[0].fair_value_anchor_yen == 1500.0
    assert detail.security_analysis.fair_value_anchor_yen == 1500.0
