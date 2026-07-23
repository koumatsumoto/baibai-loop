from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.read_api import (
    list_holding_review_publications,
    list_thesis_publications,
    list_thesis_review_publications,
    thesis_publication,
)
from baibai_engine.research.store import (
    ResearchStoreService,
)

THESIS = Path("tests/fixtures/thesis/2331-decision.yaml")
REVIEW = Path("tests/fixtures/thesis/2331-decision-review.yaml")
THESIS_ID = "thesis-20260703-2331-r1"


def _seed(path: Path) -> None:
    ResearchStoreService(path).publish_thesis_with_review(
        THESIS_ID, _payload(THESIS), _payload(REVIEW)
    )


def _payload(path: Path) -> dict[str, object]:
    raw = safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def test_independent_review_rejects_wrong_thesis_revision_without_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.sqlite"
    _seed(path)
    review = _payload(REVIEW)
    review["review_id"] = "wrong-revision-review"
    review["reviewed_thesis_sha256"] = "0" * 64
    with pytest.raises(Exception, match=r"review|thesis|scenario|source|override"):
        ResearchStoreService(path).publish_review(
            THESIS_ID,
            review,
        )
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM thesis_review WHERE review_id = 'wrong-revision-review'"
            ).fetchone()
            is None
        )


def test_atomic_thesis_review_publish_rolls_back_when_review_is_wrong(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    thesis = _payload(THESIS)
    wrong_review = _payload(REVIEW)
    wrong_review["reviewed_thesis_sha256"] = "0" * 64
    with pytest.raises(Exception, match=r"thesis|scenario|source"):
        ResearchStoreService(path).publish_thesis_with_review(
            "thesis-atomic-invalid",
            thesis,
            wrong_review,
        )
    assert not path.exists()


def test_atomic_thesis_review_publish_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    service = ResearchStoreService(path)
    thesis = _payload(THESIS)
    review = _payload(REVIEW)
    service.publish_thesis_with_review(THESIS_ID, thesis, review)
    service.publish_thesis_with_review(THESIS_ID, thesis, review)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM thesis").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM thesis_review").fetchone()[0] == 1


def test_research_read_facade_returns_ids_and_uses_read_only_connection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.sqlite"
    _seed(path)
    theses = list_thesis_publications(path, ticker="2331")
    assert theses[0]["thesis_id"] == THESIS_ID
    assert isinstance(theses[0]["payload"], dict)
    assert thesis_publication(path, thesis_id=THESIS_ID) == theses[0]
    assert list_thesis_review_publications(path, thesis_id=str(theses[0]["thesis_id"]))
    assert list_holding_review_publications(path, ticker="2331") == []
    assert not (tmp_path / "missing.sqlite").exists()
    assert list_thesis_publications(tmp_path / "missing.sqlite") == []
    assert not (tmp_path / "missing.sqlite").exists()
