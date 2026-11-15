"""HTML depth must not change verified source values or numeric token boundaries."""

from contextlib import closing
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from tests.helpers.external_assessment_html import (
    kabumap_html,
    kabumap_not_computed_html,
    minkabu_html,
    monex_html,
    rakuten_html,
)
from tests.helpers.external_assessment_http import CaptureSession
from tests.helpers.external_assessment_responses import seed_response

from baibai_engine.machine_analysis.observations.external_assessments.normalization import (
    Element,
    ParseError,
    document,
    kabumap,
    minkabu,
    monex,
    numeric_text,
    rakuten,
)
from baibai_engine.machine_analysis.observations.external_assessments.rebuild import (
    rebuild_attempts,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.external_assessments.collection import collect

RECEIVED = datetime(2026, 10, 11, tzinfo=UTC)


def nested(html, depth=1200):
    return "<div>" * depth + html + "</div>" * depth


def test_text_find_and_numeric_text_keep_preorder_and_line_breaks():
    page = document('<p>A<b>B</b><br>C<span class="x">D</span></p><p>E</p>')
    assert [node.tag for node in page.find()] == ["document", "p", "b", "br", "span", "p"]
    assert [node.text for node in page.find("p")] == ["ABCD", "E"]
    assert [node.text for node in page.find("span", **{"class": "x"})] == ["D"]
    assert page.find("span", **{"class": "y"}) == []
    assert page.text == "ABCDE"
    assert numeric_text(page) == "AB\nCDE"


def test_numeric_break_skips_children_without_changing_plain_text_semantics():
    # Preserve the helper contract even for an explicitly constructed void node.
    node = Element("br", {}, ["not a numeric token"])
    assert node.text == "not a numeric token"
    assert numeric_text(node) == "\n"


@pytest.mark.parametrize("depth", [1200, 2400])
def test_deep_helpers_preserve_order_text_and_separate_numeric_tokens(depth):
    page = document(nested('<span id="number">1<br>2</span>', depth))
    assert [node.attrs for node in page.find("span")] == [{"id": "number"}]
    assert page.text == "12"
    assert numeric_text(page) == "1\n2"


@pytest.mark.parametrize("depth", [0, 1200])
def test_quants_visible_text_keeps_excluded_subtrees_and_whitespace_rules(depth):
    page = document(
        nested(
            " A <head>hidden</head><span>B</span><script>hidden</script>"
            "<style>hidden</style><template><b>hidden</b></template> C ",
            depth,
        )
    )
    assert kabumap._visible_text(page) == "ABC"


@pytest.mark.parametrize("source", ["monex", "kabumap", "not_computed", "rakuten", "minkabu"])
def test_deep_layout_keeps_original_source_values_times_and_parser_version(source):
    parser, html = {
        "monex": (monex, monex_html("7203")),
        "kabumap": (kabumap, kabumap_html("7203")),
        "not_computed": (kabumap, kabumap_not_computed_html("7203")),
        "rakuten": (rakuten, rakuten_html("7203")),
        "minkabu": (minkabu, minkabu_html("7203")),
    }[source]
    url = (
        rakuten.IFRAME_BASE + rakuten.IFRAME_PARAMETER + "7203.T"
        if source == "rakuten"
        else parser.public_url("7203")
    )
    kwargs = dict(ticker="7203", source_url=url, fetched_at_utc=RECEIVED)
    assert parser.parse_html(nested(html), **kwargs) == parser.parse_html(html, **kwargs)


@pytest.mark.parametrize(("parser", "html"), [(monex, monex_html), (kabumap, kabumap_html)])
def test_deep_layout_does_not_relax_ticker_identity(parser, html):
    with pytest.raises(ParseError, match="ticker_identity"):
        parser.parse_html(
            nested(html("9432")),
            ticker="7203",
            source_url=parser.public_url("7203"),
            fetched_at_utc=RECEIVED,
        )


def test_collection_preserves_both_originals_and_observations_with_deep_first_page():
    first = nested(monex_html("7203"))
    received = []
    session = CaptureSession([(200, first), (200, monex_html("9432"))])
    result = collect(
        ["7203", "9432"], sources=("monex",), session=session, on_received_html=received.append
    )
    assert [item.ticker for item in result.observations] == ["7203", "9432"]
    assert [item.status for item in result.outcomes] == ["observed", "observed"]
    assert len(received) == len(session.calls) == 2
    assert received[0].body == first.encode()
    assert result.stop_reason is None


def test_explicit_deep_raw_replay_preserves_receipts_and_original_time(tmp_path):
    path = tmp_path / "private.sqlite"
    attempt = str(uuid4())
    with closing(open_connection(path)) as conn:
        seed_response(
            conn,
            attempt_id=attempt,
            source="monex",
            requested_ticker="7203",
            role="page",
            parent_receipt_id=None,
            requested_url=monex.public_url("7203"),
            received_at_utc=RECEIVED,
            selected_encoding="utf-8",
            body=nested(monex_html()).encode(),
        )
        before = conn.execute("SELECT * FROM equity_web_source_responses").fetchall()
    assert rebuild_attempts(path, attempt_ids=[attempt]) == 1
    with closing(open_connection(path)) as conn:
        assert conn.execute("SELECT * FROM equity_web_source_responses").fetchall() == before
        assert conn.execute(
            "SELECT ticker, fetched_at_utc, consensus_target "
            "FROM external_assessment_model_valuation_observations"
        ).fetchall() == [("7203", "2026-10-11T00:00:00.000000+00:00", 1234)]
