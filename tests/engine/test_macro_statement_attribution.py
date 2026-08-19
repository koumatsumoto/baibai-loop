"""A statement's claim has to be one its citations support, not merely resolve to."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from tests.helpers.macro_context import macro_context_payload

from baibai_engine.macro.context.models import (
    MacroContextDocument,
    require_attributed_statements,
    unattributed_statements,
)

_ARTICLE_ID = "a-tsr-bankruptcy-202607"


def _article(**overrides: Any) -> dict[str, Any]:
    article: dict[str, Any] = {
        "input_id": _ARTICLE_ID,
        "source": "東京商工リサーチ",
        "title": "2026年7月の全国企業倒産",
        "url": "https://www.tsr-net.co.jp/data/detail/1200000_1527.html",
        "published_at": "2026-08-10T14:00:00+09:00",
        "accessed_at": "2026-08-17T12:00:00+09:00",
        "status": "ok",
        "used_for": "倒産件数と販売不振比率の確認",
        "identifiers": ["77.3%", "東京商工リサーチ"],
    }
    article.update(overrides)
    return article


def _document(*, summary: str, source_ids: list[str], **article_overrides: Any) -> Any:
    payload = macro_context_payload()
    payload["inputs"]["articles"] = [_article(**article_overrides)]
    fact = payload["core"][0]["fact_summary"][0]
    fact["summary"] = summary
    fact["source_ids"] = source_ids
    return MacroContextDocument.model_validate(payload)


def test_a_statement_restating_an_articles_figure_must_cite_that_article() -> None:
    """The shape that actually occurred: fourteen statements restated a figure from one
    article while citing only the series input beside it, and `publish --check` returned
    ok every time."""

    document = _document(
        summary="販売不振型が 77.3% を占め、需要側の弱さが主因である。",
        source_ids=["us-10y"],
    )

    failures = unattributed_statements(document)

    assert len(failures) == 1
    assert "'77.3%'" in failures[0]
    assert _ARTICLE_ID in failures[0]
    with pytest.raises(ValueError, match="without citing"):
        require_attributed_statements(document)


def test_the_same_statement_passes_once_it_cites_the_article() -> None:
    document = _document(
        summary="販売不振型が 77.3% を占め、需要側の弱さが主因である。",
        source_ids=["us-10y", _ARTICLE_ID],
    )

    assert unattributed_statements(document) == ()


def test_a_statement_that_never_uses_the_identifier_is_left_alone() -> None:
    document = _document(
        summary="倒産の水準は前年並みで、資金繰りの逼迫は広がっていない。",
        source_ids=["us-10y"],
    )

    assert unattributed_statements(document) == ()


def test_a_numeric_identifier_inside_a_longer_number_is_not_a_use() -> None:
    """The noise that made the first hand-run sweep return 33 hits for 14 real gaps:
    `8-1` sits inside `2026-08-17`, and a substring match calls that a citation gap."""

    document = _document(
        summary="2026-08-17 時点の判断である。",
        source_ids=["us-10y"],
        identifiers=["8-1"],
    )

    assert unattributed_statements(document) == ()


def test_the_same_identifier_used_on_its_own_is_a_use() -> None:
    """The guard must not swallow the real case it exists to separate from."""

    document = _document(
        summary="決定は 8-1 で、反対は 1 名だった。",
        source_ids=["us-10y"],
        identifiers=["8-1"],
    )

    failures = unattributed_statements(document)

    assert len(failures) == 1
    assert "'8-1'" in failures[0]


def test_an_article_that_declares_nothing_checks_nothing() -> None:
    document = _document(
        summary="販売不振型が 77.3% を占める。",
        source_ids=["us-10y"],
        identifiers=[],
    )

    assert unattributed_statements(document) == ()


def test_a_failed_article_does_not_bind_statements() -> None:
    """A citation to a failed input is already refused elsewhere; requiring one here
    would make the two gates ask for opposite things."""

    document = _document(
        summary="販売不振型が 77.3% を占める。",
        source_ids=["us-10y"],
        status="failed",
    )

    assert unattributed_statements(document) == ()


def test_prose_beyond_the_summary_is_read_too() -> None:
    """A statement's claim is not confined to its `summary`. Reading only that field
    would leave every falsifier, transmission and counter-evidence line unchecked, which
    is where the 2026-08-17 gaps concentrated."""

    payload = macro_context_payload()
    payload["inputs"]["articles"] = [_article()]
    risk = payload["core"][8]["risk_environment"]
    risk["summary"] = "倒産の内訳を確認した。"
    risk["falsifiers"] = ["販売不振型が 77.3% を割り込むこと"]
    risk["source_ids"] = ["us-10y"]

    failures = unattributed_statements(MacroContextDocument.model_validate(payload))

    assert len(failures) == 1
    assert "'77.3%'" in failures[0]


@pytest.mark.parametrize("value", ["", " ", "7"])
def test_an_identifier_too_short_to_be_distinctive_is_refused(value: str) -> None:
    payload = macro_context_payload()
    payload["inputs"]["articles"] = [_article(identifiers=[value])]

    with pytest.raises(ValidationError):
        MacroContextDocument.model_validate(payload)


def test_duplicate_identifiers_are_refused() -> None:
    payload = macro_context_payload()
    payload["inputs"]["articles"] = [_article(identifiers=["77.3%", "77.3%"])]

    with pytest.raises(ValidationError):
        MacroContextDocument.model_validate(payload)
