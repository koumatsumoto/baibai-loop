from __future__ import annotations

import re
from datetime import date, timedelta

# S&P Global PMI press releases state the headline index in a bounded context
# around "the headline ... PMI". Extraction is scoped to that context and every
# candidate is range-checked so a misparse can never enter the store as data.
_PLAUSIBLE_MIN = 30.0
_PLAUSIBLE_MAX = 70.0


class PmiExtractionError(RuntimeError):
    """Raised when a PMI value cannot be read unambiguously from release text."""


def _previous_month(value: date) -> date:
    return (value.replace(day=1) - timedelta(days=1)).replace(day=1)


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

    headline_match = re.search(r"\bthe headline\b", normalized_text, re.IGNORECASE)
    if headline_match is None:
        return None
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
    month = re.escape(expected_observed_at.strftime("%B"))
    patterns = (
        re.compile(rf"(?P<value>\d{{2}}\.\d)\s+(?:(?:in|for)\s+)?{month}\b", re.IGNORECASE),
        re.compile(rf"\b{month}(?:['’]s)?\s+(?P<value>\d{{2}}\.\d)\b", re.IGNORECASE),
        re.compile(
            rf"\b{month}\s+(?:reading|figure|level|index)\s+"
            rf"(?:at|of|was|recorded)\s+(?P<value>\d{{2}}\.\d)\b",
            re.IGNORECASE,
        ),
    )
    candidates = {
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
