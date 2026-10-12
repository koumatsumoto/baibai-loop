"""Synthetic exploration regressions; these malformed forms were not found live."""

from datetime import UTC, datetime

import pytest
from tests.engine.test_external_assessment_sources import (
    monex_html,
    rakuten_html,
    rakuten_no_estimates_html,
)

from baibai_engine.external_assessment.acquisition.sources import ParseError, monex, rakuten

RECEIVED = datetime(2026, 9, 30, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    "request_context",
    [
        '<meta property="og:url" content="https://www.rakuten-sec.co.jp/web/market/search/forecast.html?ric=7203.T">',
        '<a href="index.php?action=tp4&amp;sa=report_est&amp;bcode=7203.T">合成リンク</a>',
        '<script>var bcode = "7203.T";</script>',
    ],
)
def test_no_estimates_shell_with_request_context_cannot_prove_response_identity(request_context):
    # Unlike a chart constructor or the comparison's own row, a request locator
    # copied into a generic shell does not demonstrate an observation's ticker.
    html = rakuten_no_estimates_html().replace("</head>", request_context + "</head>")
    with pytest.raises(ParseError, match=r"^no_estimates_identity_unconfirmed$"):
        rakuten.parse_html(
            html,
            ticker="7203",
            source_url=rakuten.IFRAME_BASE + rakuten.IFRAME_PARAMETER + "7203.T",
            fetched_at_utc=RECEIVED,
        )


def observed_value(source, value):
    if source == "monex":
        observed = monex.parse_html(
            monex_html("130A", value=value),
            ticker="130A",
            source_url=monex.public_url("130A"),
            fetched_at_utc=RECEIVED,
        )
        return observed.consensus_target
    observed = rakuten.parse_html(
        rakuten_html("130A", value=value),
        ticker="130A",
        source_url=rakuten.IFRAME_BASE + rakuten.IFRAME_PARAMETER + "130A.T",
        fetched_at_utc=RECEIVED,
    )
    return observed.rating


@pytest.mark.parametrize("source", ["monex", "rakuten"])
@pytest.mark.parametrize("malformed", ["1 2", "1234,567"])
def test_ambiguous_numeric_format_is_parse_failure(source, malformed):
    value = malformed + "円" if source == "monex" else malformed
    with pytest.raises(ParseError, match=r"^unexpected_numeric_cell$"):
        observed_value(source, value)


@pytest.mark.parametrize("source", ["monex", "rakuten"])
@pytest.mark.parametrize(
    ("numeric", "expected"),
    [
        ("1234567", 1234567),
        ("1,234,567", 1234567),
        ("-50", -50),
        ("-1,234.75", -1234.75),
        ("0", 0),
        (" \n1,234\t ", 1234),
    ],
)
def test_display_numeric_grammar_preserves_meaning(source, numeric, expected):
    # Outer whitespace and whitespace before the currency unit are formatting.
    value = numeric + "円 \n" if source == "monex" else numeric
    assert observed_value(source, value) == expected


def test_partial_missing_zero_and_negative_models_keep_distinct_values():
    html = (
        monex_html("130A")
        .replace("1,234円", "0円", 1)
        .replace("1,234円", "－", 1)
        .replace("1,234円", " -12.75\t円 ", 1)
        .replace("1,234円", "--", 1)
    )
    observed = monex.parse_html(
        html,
        ticker="130A",
        source_url=monex.public_url("130A"),
        fetched_at_utc=RECEIVED,
    )
    assert observed.per_company_forecast == 0
    assert observed.per_consensus is None
    assert observed.pbr == -12.75
    assert observed.consensus_target is None


@pytest.mark.parametrize("source", ["monex", "rakuten"])
def test_currency_suffix_does_not_turn_unknown_numeric_into_normal_missing(source):
    with pytest.raises(ParseError, match=r"^unexpected_numeric_cell$"):
        observed_value(source, "－円")
