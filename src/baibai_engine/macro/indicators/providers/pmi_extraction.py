from __future__ import annotations

import re
from datetime import date, timedelta

# S&P Global PMI press releases state the headline index in a bounded context
# around "the headline ... PMI". Extraction is scoped to that context and every
# candidate is range-checked so a misparse can never enter the store as data.
# The house style varies across series and years — some releases drop "the
# headline" and lead with the index name, and the services releases phrase it as
# "the headline index posted X in Month" without repeating "PMI" — so a primary
# statement is also read directly: an index anchor, a reporting verb, then the
# value immediately tied to the expected month. Every source contributes to one
# candidate set, and a disagreement raises rather than guesses.
_PLAUSIBLE_MIN = 30.0
_PLAUSIBLE_MAX = 70.0

# Verbs S&P Global uses to state a headline reading (e.g. "posted 53.2", "rose to
# 51.0", "posted at the neutral level of 50.0"); a short non-digit gap after the
# verb absorbs qualifiers before the number.
_HEADLINE_VERBS = (
    r"posted|registered|recorded|came in at|stood at|was|remained at|"
    r"rose to|climbed to|increased to|grew to|"
    r"fell to|dropped to|declined to|slipped to|moved to|dipped to|"
    r"edged up to|edged down to"
)
# Index anchors that name the headline series (not a sub-index like new orders or
# employment), so a primary statement is matched only against the headline.
_INDEX_ANCHOR = (
    r"(?:Business Activity Index|Purchasing Managers[’'`]?\s*Index|"
    r"Manufacturing PMI|Services PMI)"
)


class PmiExtractionError(RuntimeError):
    """Raised when a PMI value cannot be read unambiguously from release text."""


def _previous_month(value: date) -> date:
    return (value.replace(day=1) - timedelta(days=1)).replace(day=1)


def _primary_statement_candidates(normalized_text: str, expected_observed_at: date) -> set[float]:
    """Read the headline value from the primary statement, month-anchored.

    Matches either "the headline ... <verb> <value> in <month>" or
    "<index anchor> ... <verb> <value> in <month>", requiring the value to sit
    immediately before the expected month so a comparison clause for another
    month (or a sub-index) is never picked up.
    """
    month = re.escape(expected_observed_at.strftime("%B"))
    tail = rf"(?P<value>\d{{2}}\.\d)\s+(?:in\s+)?{month}\b"
    patterns = (
        re.compile(
            rf"\bthe headline\b[^.]{{0,120}}?\b(?:{_HEADLINE_VERBS})\b[^.\d]{{0,25}}?{tail}",
            re.IGNORECASE,
        ),
        re.compile(
            rf"{_INDEX_ANCHOR}[^.]{{0,55}}?\b(?:{_HEADLINE_VERBS})\b[^.\d]{{0,25}}?{tail}",
            re.IGNORECASE,
        ),
    )
    return {
        float(match.group("value"))
        for pattern in patterns
        for match in pattern.finditer(normalized_text)
    }


# Names that denote the headline series (not a sub-index such as new orders or
# employment), used to scope the order-independent fallback below.
_HEADLINE_ANCHOR = re.compile(
    r"the headline|Business Activity Index|Purchasing Managers|"
    r"Services PMI|Manufacturing PMI|\bthe PMI\b",
    re.IGNORECASE,
)


# The Composite index value is published in the same release as the headline and reads
# just like it ("Composite PMI Output Index posted X in Month"); blanking that clause
# keeps it from colliding with the manufacturing/services headline. Anchored on a verb
# so it removes only the composite's own value statement, and works even when broken
# sentence segmentation glues the contact block to the body.
_COMPOSITE_VALUE = re.compile(
    r"Composite PMI[^.]{0,70}?\b(?:posted|registered|recorded|increased to|rose to|"
    r"climbed to|edged up to|edged down to|fell to|dropped to|declined to|slipped to|"
    r"came in at|stood at|was|of|at)\s+\d{2}\.\d\s+(?:in|for)\s+\w+",
    re.IGNORECASE,
)


def _strip_composite(normalized_text: str) -> str:
    """Blank the Composite index's own value statement so it cannot be read as the
    manufacturing/services headline. The headline value is never stated inside a
    Composite value clause, so this does not drop it."""
    return _COMPOSITE_VALUE.sub(" ", normalized_text)


def _anchored_month_candidates(normalized_text: str, expected_observed_at: date) -> set[float]:
    """Fallback: a value tied to the expected month inside a sentence that names the
    headline series, regardless of clause order.

    Covers phrasings the primary statement misses because the value leads the clause
    ("At 52.4 in April, the headline ... Index rose ..."), or is stated as a transition
    ("from X in March to 51.0 in May") or a level ("the neutral value of 50.0 in May").
    Requiring a headline anchor in the same sentence (never a bare "index") keeps a
    sub-index reading out, and the month must sit immediately after the value.
    """
    month = re.escape(expected_observed_at.strftime("%B"))
    value_pat = re.compile(rf"(?P<value>\d{{2}}\.\d)\s+(?:in|for)\s+{month}\b", re.IGNORECASE)
    out: set[float] = set()
    for sentence in re.split(r"(?<=[.!?])\s+", normalized_text):
        if _HEADLINE_ANCHOR.search(sentence):
            out |= {float(match.group("value")) for match in value_pat.finditer(sentence)}
    return out


def extract_pmi_value(
    normalized_text: str,
    *,
    expected_observed_at: date,
    release_observed_at: date,
) -> float | None:
    """Extract a headline PMI value for ``expected_observed_at`` from release text.

    ``normalized_text`` is the whitespace-collapsed PDF text. Returns ``None`` when
    the month is not present; raises :class:`PmiExtractionError` when the text is
    internally inconsistent (conflicting values) or the value is implausible.
    """

    normalized_text = _strip_composite(normalized_text)
    month = re.escape(expected_observed_at.strftime("%B"))
    # The primary statement is read from the whole release first and does not
    # depend on a "the headline ... PMI" scope, because older releases and the
    # services house style phrase the value sentence without repeating "PMI".
    # Month-anchoring keeps this from catching another month's value.
    candidates = _primary_statement_candidates(normalized_text, expected_observed_at)

    # The scoped patterns and the restated-month fallbacks anchor on "the
    # headline"; run them only when that anchor is present. They cover phrasings
    # (e.g. "<Month> reading of X", or a prior month restated in the next
    # release) that the primary statement does not.
    headline_match = re.search(r"\bthe headline\b", normalized_text, re.IGNORECASE)
    sentences: list[str] = []
    if headline_match is not None:
        paragraph = normalized_text[
            max(0, headline_match.start() - 120) : headline_match.start() + 1_200
        ]
        sentences = re.split(r"(?<=[.!?])\s+", paragraph)
        scoped_sentences: list[str] = []
        for index, sentence in enumerate(sentences):
            if not (
                re.search(r"\bthe headline\b", sentence, re.IGNORECASE)
                and re.search(r"\bPMI\b", sentence, re.IGNORECASE)
            ):
                continue
            scoped_sentences.append(sentence)
            if index + 1 < len(sentences) and re.search(
                r"\bthe (?:index|figure|reading)\b",
                sentences[index + 1],
                re.IGNORECASE,
            ):
                scoped_sentences.append(sentences[index + 1])
        scope = " ".join(scoped_sentences)
        patterns = (
            re.compile(rf"(?P<value>\d{{2}}\.\d)\s+(?:(?:in|for)\s+)?{month}\b", re.IGNORECASE),
            re.compile(rf"\b{month}(?:['’]s)?\s+(?P<value>\d{{2}}\.\d)\b", re.IGNORECASE),
            re.compile(
                rf"\b{month}\s+(?:reading|figure|level|index)\s+"
                rf"(?:at|of|was|recorded)\s+(?P<value>\d{{2}}\.\d)\b",
                re.IGNORECASE,
            ),
        )
        candidates |= {
            float(match.group("value")) for pattern in patterns for match in pattern.finditer(scope)
        }

    if not candidates and expected_observed_at == release_observed_at:
        release_month = re.escape(release_observed_at.strftime("%B"))
        for index, sentence in enumerate(sentences[:-1]):
            if (
                re.search(r"\bthe headline\b", sentence, re.IGNORECASE)
                and re.search(r"\bPMI\b", sentence, re.IGNORECASE)
                and re.search(rf"\b{release_month}\b", sentence, re.IGNORECASE)
            ):
                linked_match = re.search(
                    r"(?:\bthe index\s+(?:recorded|posted)|"
                    r"\b(?:falling|rising)\s+to)\s+(?P<value>\d{2}\.\d)\b",
                    sentences[index + 1],
                    re.IGNORECASE,
                )
                if linked_match is not None:
                    candidates.add(float(linked_match.group("value")))

    if not candidates and expected_observed_at == _previous_month(release_observed_at):
        release_month = re.escape(release_observed_at.strftime("%B"))
        for index, sentence in enumerate(sentences):
            if (
                re.search(r"\bthe headline\b", sentence, re.IGNORECASE)
                and re.search(r"\bPMI\b", sentence, re.IGNORECASE)
                and re.search(rf"\b{release_month}\b", sentence, re.IGNORECASE)
            ):
                candidates.update(
                    float(match.group("value"))
                    for match in re.finditer(
                        r"\bfrom\s+(?P<value>\d{2}\.\d)\b", sentence, re.IGNORECASE
                    )
                )
                if index + 1 < len(sentences):
                    linked_match = re.search(
                        r"\bthe index\s+(?:recorded|posted)\s+\d{2}\.\d"
                        r"\s*,?\s*(?:up|down)?\s*from\s+(?P<value>\d{2}\.\d)\b",
                        sentences[index + 1],
                        re.IGNORECASE,
                    )
                    if linked_match is not None:
                        candidates.add(float(linked_match.group("value")))

    if not candidates and expected_observed_at == release_observed_at:
        # Order-independent last resort for house styles that lead with the value
        # or state it as a transition/level rather than "<verb> X in Month". Gated
        # to the release's own month so a "from X in <prior month>" comparison in a
        # restating release is never mistaken for the prior month's own reading.
        candidates |= _anchored_month_candidates(normalized_text, expected_observed_at)

    if len(candidates) > 1:
        raise PmiExtractionError(
            f"conflicting PMI values for {expected_observed_at}: {sorted(candidates)}"
        )
    if not candidates:
        return None
    value = candidates.pop()
    if not _PLAUSIBLE_MIN <= value <= _PLAUSIBLE_MAX:
        raise PmiExtractionError(
            f"PMI value outside plausible range [{_PLAUSIBLE_MIN}, {_PLAUSIBLE_MAX}]: {value}"
        )
    return value
