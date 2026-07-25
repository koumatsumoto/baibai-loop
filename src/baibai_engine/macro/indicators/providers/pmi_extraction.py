from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from datetime import date, timedelta

# A S&P Global PMI release states its headline reading once, in its opening
# statement, surrounded by numbers that are not the reading: the previous month's
# level, the flash estimate, the 50.0 no-change threshold, the composite index and
# the sub-indices. Extraction is therefore a matter of proving which number is the
# reading, and the rules here are built so that an unproven number is dropped rather
# than guessed:
#
# * a value is read only inside the opening statement, and only where the release
#   ties it to the month wanted or introduces it as that month's reading;
# * a statement that names a sub-index or the composite index is not read at all, and
#   numbers that can never be a reading (a comparison threshold, a flash estimate, a
#   multi-month average) are removed from the text before any rule runs;
# * readers run strictest first, and the readers that cannot prove the month from the
#   text are gated on which month the release reports, so an unanchored reading can
#   only ever be attributed to that release's own month;
# * the values a reader finds must agree with each other and sit in the plausible
#   index range, so a misparse raises instead of entering the store as data.
__all__ = ["PmiExtractionError", "extract_pmi_value"]

_PLAUSIBLE_MIN = 30.0
_PLAUSIBLE_MAX = 70.0

# A headline reading is two digits and one decimal. pypdf sometimes splits the
# decimal point off its digits ("47 .9"), so the token tolerates one space on each
# side of the point; the lookarounds keep it from matching part of a longer number
# such as a year or a two-decimal figure.
_VALUE = r"(?<![\d.])\d{2}\s?\.\s?\d(?!\d)"
# A gap that stays inside one sentence. A decimal point between digits is let
# through, so a value standing in the gap ("slipped from 53.2 in November to 51.6")
# does not read as a sentence end.
_SAME_SENTENCE = r"(?:[^.]|(?<=\d)\.(?=\d))"
# The reach from a movement verb to the level it moved to. It stops at a comparison
# word, because a comparison borrows the same preposition without stating a reading
# ("improved in October, compared to 52.0 in September").
_MOVEMENT_REACH = rf"(?:(?!compared|close|relative|similar|prior){_SAME_SENTENCE}){{0,60}}?"

# Verbs that state a level ("posted 53.2", "recording 50.2", "reaching a 33-month
# high of 56.8") and verbs that state a movement whose destination is the level
# ("rose to 54.4", "slipped from 53.2 in November to 51.6").
_STATING_VERBS = (
    r"posted|posting|registered|registering|recorded|recording|"
    r"reached|reaching|hit|hitting|came in at|stood at"
)
_MOVEMENT_VERBS = (
    r"rose|rising|fell|falling|climbed|climbing|dropped|dropping|slipped|slipping|"
    r"declined|declining|increased|increasing|decreased|decreasing|improved|improving|"
    r"worsened|worsening|moved|moving|dipped|dipping|slumped|slumping|jumped|jumping|"
    r"eased|easing|edged\s+(?:up|down)|edging\s+(?:up|down)"
)
# A record level is named before its value ("a 33-month high of 56.8"), so the
# stating and movement constructions both allow that phrase before the number.
_RECORD_LEVEL = r"(?:a\s+[\w-]+\s+(?:high|low)\s+of\s+)?"

# Names of the headline index. "the ... index" is included because the statement
# often refers back to it that way ("the respective seasonally adjusted index
# climbed ...").
_HEADLINE_INDEX = re.compile(
    r"the headline\b|Business Activity Index|Purchasing Managers[’'`]?\s*Index|"
    r"Manufacturing PMI|Services PMI|\bthe PMI\b|\bthe(?:\s+[\w’'-]+){0,3}\s+index\b",
    re.IGNORECASE,
)
# Names of the indices published beside the headline. Each is matched as a full index
# name, so prose about employment or new orders does not disqualify a statement while
# the composite index and the sub-indices still cannot be read as the headline.
_SUB_INDEX = re.compile(
    r"(?:New Orders|New Business|New Export (?:Orders|Business)|Employment|Output|"
    r"Suppliers[’'`]?\s*Delivery Times|Stocks of (?:Purchases|Finished Goods)|"
    r"Quantity of Purchases|Input Prices|Prices Charged|Output Charges|Selling Prices|"
    r"Backlogs of Work|Future Output|Business Expectations)\s+Index",
    re.IGNORECASE,
)
# A sentence continues the headline statement by naming what it is talking about
# ("That said, at 51.6 the index was down ...", "The reading was down from 51.9 in
# March"), which is how a continuation is told apart from the next subject.
_CARRIES_STATEMENT_ON = re.compile(
    r"\bthe\s+(?:[\w’'-]+\s+){0,2}(?:index|reading|figure|PMI)\b",
    re.IGNORECASE,
)

# The releases are published in English, so the month names are fixed here rather
# than taken from the runtime locale.
_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

# A value governed by a comparison is the threshold the reading is measured against
# ("above the 50.0 no-change mark", "below 50.0"), never the reading itself.
_COMPARISON_VALUE = re.compile(
    rf"\b(?:above|below|beneath|under|over)\s+(?:the\s+)?"
    rf"(?:crucial\s+|critical\s+|key\s+)?{_VALUE}",
    re.IGNORECASE,
)
# The flash estimate is an earlier, provisional reading of the same month; keeping it
# would leave two rival numbers for one month.
_FLASH_VALUE = re.compile(
    rf"[‘'’]?flash[’'‘]?\s+(?:estimate|reading|figure|projection)\s+of\s+{_VALUE}",
    re.IGNORECASE,
)
# An average over a span of months ("has trended at 53.7 from January to November")
# is not any single month's reading.
_SPAN_AVERAGE = re.compile(
    rf"\b(?:trended|trending|averaged|averaging)\s+(?:at\s+)?{_VALUE}",
    re.IGNORECASE,
)
_NOT_A_READING = (_COMPARISON_VALUE, _FLASH_VALUE, _SPAN_AVERAGE)


class PmiExtractionError(RuntimeError):
    """Raised when a PMI value cannot be read unambiguously from release text."""


def extract_pmi_value(
    normalized_text: str,
    *,
    expected_observed_at: date,
    release_observed_at: date,
) -> float | None:
    """Extract the headline PMI value for ``expected_observed_at`` from release text.

    ``normalized_text`` is the whitespace-collapsed PDF text and
    ``release_observed_at`` is the month the release reports, which is the month
    after ``expected_observed_at`` when a month is taken from the release that
    restates it. Returns ``None`` when no reader can prove a value for the month, and
    raises :class:`PmiExtractionError` when readings disagree or a reading lies
    outside the plausible index range.
    """

    text = _readable_text(normalized_text)
    windows = _statement_windows(text)
    for read in _readers(expected=expected_observed_at, reported=release_observed_at):
        for window in windows:
            candidates = read(window)
            if candidates:
                return _single_reading(candidates, expected=expected_observed_at)
    return None


def _readers(*, expected: date, reported: date) -> Iterator[Callable[[str], set[float]]]:
    """The readers that may speak for ``expected``, strictest first.

    The month-anchored reader always applies because it proves the month from the
    text itself. The other two cannot, so each is gated on how ``expected`` relates
    to the month the release reports: a reading the statement introduces without a
    month can only be that release's own month, and a bare comparison ("down from
    52.7") can only be the month before it.
    """

    month = _month_pattern(expected)
    yield lambda window: _month_anchored(window, month=month)
    if expected == reported:
        yield _introduced_reading
    elif expected == _previous_month(reported):
        yield lambda window: _restated_previous(window, reported=_month_pattern(reported))


def _month_anchored(window: str, *, month: str) -> set[float]:
    """Values the release ties to ``month`` inside ``window``.

    The month sits after its value ("posted 47.9 in December", "to 48.3 November"),
    before it as a possessive ("down from October's 54.8"), or before it in a
    movement ("fell for the third month running in April to 51.3"). The movement
    carries no digit between the verb and the month, so a transition through another
    month ("rose from 47.2 in March to 49.6") cannot be read as that month's own
    reading.

    A reading that lands exactly on the no-change threshold is stated as equal to it
    ("posted in line with the 50.0 no-change mark in April"). That counts as the
    reading because the verb sits next to "in line with", so the hedged wording the
    releases use for an approximation ("broadly in line with") does not match, and
    neither does a threshold the reading is merely compared against.
    """

    patterns = (
        rf"(?P<value>{_VALUE})\s+(?:(?:in|for|during)\s+)?{month}\b",
        rf"\b{month}(?:['’]s)?\s+(?P<value>{_VALUE})\b",
        rf"\b(?:{_MOVEMENT_VERBS})\b[^.\d]{{0,60}}?"
        rf"\bin\s+{month}\s+to\s+{_RECORD_LEVEL}(?P<value>{_VALUE})\b",
        rf"\b(?:{_STATING_VERBS}|came in|was)\s+in\s+line\s+with\s+the\s+"
        rf"(?P<value>{_VALUE})\s+no[-\s]change\s+mark\s+(?:in|for|during)\s+{month}\b",
    )
    return _matched_values(patterns, window)


def _introduced_reading(window: str) -> set[float]:
    """Values the statement introduces as the reading without naming the month.

    Three constructions carry the reading: it is stated ("posted 52.3", "recording
    50.2", "reaching a 33-month high of 56.8"), it is the destination of a movement
    ("rose to 54.4", "slipped from 53.2 in November to 51.6"), or it is given as a
    level ("at 51.6 the index was down from 53.8 in September"). A number that is not
    the reading arrives through "from", through "of" after a noun such as estimate or
    reading, or after a comparison, and none of those are matched here.
    """

    patterns = (
        rf"\b(?:{_STATING_VERBS})\s+{_RECORD_LEVEL}(?P<value>{_VALUE})\b",
        rf"\b(?:{_MOVEMENT_VERBS})\b{_MOVEMENT_REACH}\bto\s+{_RECORD_LEVEL}(?P<value>{_VALUE})\b",
        rf"\bat\s+{_RECORD_LEVEL}(?P<value>{_VALUE})\b",
    )
    return _matched_values(patterns, window)


def _restated_previous(window: str, *, reported: str) -> set[float]:
    """The reading a release restates for the month before it, without naming it.

    The opening statement compares the release's own reading against the month before
    ("Recording 50.2, down from 52.7"), so inside a statement that names the release's
    own month a bare "from <value>" is the previous month's reading.
    """

    if re.search(rf"\b{reported}\b", window, re.IGNORECASE) is None:
        return set()
    return _matched_values((rf"\bfrom\s+(?P<value>{_VALUE})\b",), window)


def _statement_windows(text: str) -> tuple[str, ...]:
    """The stretches of the release that can state the headline reading, in order.

    A window is a sentence naming the headline index joined with the sentence after
    it, because the reading is stated either there or in the sentence that carries
    the statement on ("... remained above the no-change mark in October. That said,
    at 51.6 the index was down from 53.8 in September."). Reading the first window
    that yields a value skips the chart captions and the methodology note without
    having to recognise them, and keeps a recap later in the release from outweighing
    the statement.

    The following sentence joins the window only when it refers back to the index it
    continues, so a sentence that moves on to another subject contributes nothing.
    Neither half of a window may name an index published beside the headline, so a
    sub-index or composite level is never a candidate for the headline series.
    """

    sentences = re.split(r"(?<=[.!?])\s+", text)
    windows: list[str] = []
    for index, sentence in enumerate(sentences):
        if _HEADLINE_INDEX.search(sentence) is None or _SUB_INDEX.search(sentence) is not None:
            continue
        window = [sentence]
        following = sentences[index + 1] if index + 1 < len(sentences) else ""
        if _CARRIES_STATEMENT_ON.search(following) and _SUB_INDEX.search(following) is None:
            window.append(following)
        windows.append(" ".join(window))
    return tuple(windows)


def _readable_text(normalized_text: str) -> str:
    """The release text with every number that cannot be a reading removed."""

    text = normalized_text
    for pattern in _NOT_A_READING:
        text = pattern.sub(" ", text)
    return text


def _matched_values(patterns: tuple[str, ...], window: str) -> set[float]:
    return {
        _to_float(match.group("value"))
        for pattern in patterns
        for match in re.finditer(pattern, window, re.IGNORECASE)
    }


def _single_reading(candidates: set[float], *, expected: date) -> float:
    if len(candidates) > 1:
        raise PmiExtractionError(f"conflicting PMI values for {expected}: {sorted(candidates)}")
    value = candidates.pop()
    if not _PLAUSIBLE_MIN <= value <= _PLAUSIBLE_MAX:
        raise PmiExtractionError(
            f"PMI value outside plausible range [{_PLAUSIBLE_MIN}, {_PLAUSIBLE_MAX}]: {value}"
        )
    return value


def _to_float(raw: str) -> float:
    return float(re.sub(r"\s", "", raw))


def _month_pattern(value: date) -> str:
    return _MONTH_NAMES[value.month - 1]


def _previous_month(value: date) -> date:
    return (value.replace(day=1) - timedelta(days=1)).replace(day=1)
