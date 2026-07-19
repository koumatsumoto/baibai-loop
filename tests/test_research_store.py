from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.read_api import (
    list_holding_review_publications,
    list_research_packet_publications,
    list_research_review_publications,
    research_packet_publication,
)
from baibai_engine.research.store import (
    ResearchStoreService,
)

PACKET = Path("tests/fixtures/decision-packet/2331-decision.yaml")
REVIEW = Path("tests/fixtures/decision-packet/2331-decision-review.yaml")
PACKET_ID = "packet-20260703-2331-r1"


def _seed(path: Path) -> None:
    ResearchStoreService(path).publish_packet_with_review(
        PACKET_ID, _payload(PACKET), _payload(REVIEW)
    )


def _payload(path: Path) -> dict[str, object]:
    raw = safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def test_independent_review_rejects_wrong_packet_revision_without_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.sqlite"
    _seed(path)
    review = _payload(REVIEW)
    review["review_id"] = "wrong-revision-review"
    review["reviewed_packet_sha256"] = "0" * 64
    with pytest.raises(Exception, match=r"review|packet|scenario|source|override"):
        ResearchStoreService(path).publish_review(
            PACKET_ID,
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
    packet = _payload(PACKET)
    wrong_review = _payload(REVIEW)
    wrong_review["reviewed_packet_sha256"] = "0" * 64
    with pytest.raises(Exception, match=r"packet|scenario|source"):
        ResearchStoreService(path).publish_packet_with_review(
            "packet-atomic-invalid",
            packet,
            wrong_review,
        )
    assert not path.exists()


def test_atomic_packet_review_publish_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    service = ResearchStoreService(path)
    packet = _payload(PACKET)
    review = _payload(REVIEW)
    service.publish_packet_with_review(PACKET_ID, packet, review)
    service.publish_packet_with_review(PACKET_ID, packet, review)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM research_packet").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM research_review").fetchone()[0] == 1


def test_research_read_facade_returns_ids_and_uses_read_only_connection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.sqlite"
    _seed(path)
    packets = list_research_packet_publications(path, ticker="2331")
    assert packets[0]["packet_id"] == PACKET_ID
    assert isinstance(packets[0]["payload"], dict)
    assert research_packet_publication(path, packet_id=PACKET_ID) == packets[0]
    assert list_research_review_publications(path, packet_id=str(packets[0]["packet_id"]))
    assert list_holding_review_publications(path, ticker="2331") == []
    assert not (tmp_path / "missing.sqlite").exists()
    assert list_research_packet_publications(tmp_path / "missing.sqlite") == []
    assert not (tmp_path / "missing.sqlite").exists()
