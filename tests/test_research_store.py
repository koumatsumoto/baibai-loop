from __future__ import annotations

import json
import sqlite3
from dataclasses import astuple
from pathlib import Path

import pytest

from baibai_engine.appdb.json import canonical_json
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.read_api import (
    list_holding_review_publications,
    list_research_packet_publications,
    list_research_review_publications,
    research_packet_publication,
)
from baibai_engine.research.importer import import_research_records
from baibai_engine.research.store import (
    HoldingReviewPublication,
    PacketPublication,
    ResearchConflictError,
    ResearchStoreService,
)

RESEARCH_ROOT = Path("records/03-thesis")
POSITION_ROOT = Path("records/04-position")


def _import(path: Path):
    return import_research_records(RESEARCH_ROOT, POSITION_ROOT, db_path=path)


def _payload(pattern: str) -> dict[str, object]:
    path = next(RESEARCH_ROOT.rglob(pattern))
    raw = safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def test_real_research_import_is_complete_exact_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    first = _import(path)
    second = _import(path)
    assert astuple(first) == (2, 0, 2, 0, 1, 0)
    assert astuple(second) == (0, 2, 0, 2, 0, 1)

    source_payloads = {
        canonical_json(_payload("*-3836-decision.yaml")),
        canonical_json(_payload("*-4432-decision.yaml")),
    }
    source_reviews = {
        canonical_json(_payload("*-3836-decision-review.yaml")),
        canonical_json(_payload("*-4432-decision-review.yaml")),
    }
    holding_path = next(POSITION_ROOT.rglob("*-holding-review.yaml"))
    holding_raw = safe_load(holding_path.read_text(encoding="utf-8"))
    assert isinstance(holding_raw, dict)
    with sqlite3.connect(path) as connection:
        stored_payloads = {
            row[0] for row in connection.execute("SELECT payload FROM research_packet")
        }
        assert stored_payloads == source_payloads
        assert {
            row[0] for row in connection.execute("SELECT payload FROM research_review")
        } == source_reviews
        assert connection.execute("SELECT payload FROM holding_review").fetchone()[
            0
        ] == canonical_json(holding_raw)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_import_conflict_rolls_back_all_new_rows(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _import(path)
    service = ResearchStoreService(path)
    original = _payload("*-4432-decision.yaml")
    changed = json.loads(json.dumps(original))
    changed["judgment"]["strongest_countercase"] += " changed"

    with pytest.raises(ResearchConflictError):
        service.import_publications(
            packets=(
                PacketPublication("packet-new", original),
                PacketPublication("packet-20260714-4432-r1", changed),
            ),
            reviews=(),
            holding_reviews=(),
        )
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM research_packet WHERE packet_id = 'packet-new'"
            ).fetchone()
            is None
        )


def test_independent_review_rejects_wrong_packet_revision_without_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.sqlite"
    _import(path)
    review = _payload("*-3836-decision-review.yaml")
    review["review_id"] = "wrong-revision-review"
    with pytest.raises(Exception, match=r"review|packet|scenario|source"):
        ResearchStoreService(path).publish_review(
            "packet-20260714-4432-r1",
            review,
        )
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM research_review WHERE review_id = 'wrong-revision-review'"
            ).fetchone()
            is None
        )


def test_atomic_packet_review_publish_rolls_back_when_review_is_wrong(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _import(path)
    packet = _payload("*-3836-decision.yaml")
    wrong_review = _payload("*-4432-decision-review.yaml")
    with pytest.raises(Exception, match=r"packet|scenario|source"):
        ResearchStoreService(path).publish_packet_with_review(
            "packet-atomic-invalid",
            packet,
            wrong_review,
        )
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM research_packet WHERE packet_id = 'packet-atomic-invalid'"
            ).fetchone()
            is None
        )


def test_atomic_packet_review_publish_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    service = ResearchStoreService(path)
    packet = _payload("*-3836-decision.yaml")
    review = _payload("*-3836-decision-review.yaml")
    service.publish_packet_with_review("packet-20260714-3836-r1", packet, review)
    service.publish_packet_with_review("packet-20260714-3836-r1", packet, review)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM research_packet").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM research_review").fetchone()[0] == 1


def test_holding_review_rejects_wrong_packet_revision(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _import(path)
    payload_path = next(POSITION_ROOT.rglob("*-holding-review.yaml"))
    raw = safe_load(payload_path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    with pytest.raises(ResearchConflictError, match="ticker"):
        ResearchStoreService(path).import_publications(
            packets=(),
            reviews=(),
            holding_reviews=(
                HoldingReviewPublication(
                    "holding-wrong",
                    "packet-20260714-3836-r1",
                    raw,
                ),
            ),
        )
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM holding_review WHERE holding_review_id = 'holding-wrong'"
            ).fetchone()
            is None
        )


def test_holding_publish_source_drift_is_no_write(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _import(path)
    payload_path = next(POSITION_ROOT.rglob("*-holding-review.yaml"))
    raw = safe_load(payload_path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    with pytest.raises(Exception, match="missing"):
        ResearchStoreService(path).publish_holding_review(
            "holding-source-drift",
            "packet-20260714-4432-r1",
            raw,
            root=tmp_path,
        )
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM holding_review WHERE holding_review_id = 'holding-source-drift'"
            ).fetchone()
            is None
        )


def test_research_read_facade_returns_ids_and_uses_read_only_connection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.sqlite"
    _import(path)
    packets = list_research_packet_publications(path, ticker="4432")
    assert packets[0]["packet_id"] == "packet-20260714-4432-r1"
    assert isinstance(packets[0]["payload"], dict)
    assert research_packet_publication(path, packet_id="packet-20260714-4432-r1") == packets[0]
    assert list_research_review_publications(path, packet_id=str(packets[0]["packet_id"]))
    assert list_holding_review_publications(path, ticker="4432")[0]["packet_id"] == str(
        packets[0]["packet_id"]
    )
    assert not (tmp_path / "missing.sqlite").exists()
    assert list_research_packet_publications(tmp_path / "missing.sqlite") == []
    assert not (tmp_path / "missing.sqlite").exists()
