from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta


def _previous_month(value: date) -> date:
    return (value.replace(day=1) - timedelta(days=1)).replace(day=1)


def latest_seed_matches(
    entries: Sequence[Mapping[str, object]],
    *,
    value: float,
    source_url: str,
) -> bool:
    """Return whether the latest vintage already contains the extracted value."""
    if not entries:
        return False
    latest = max(entries, key=lambda item: _entered_at(item.get("entered_at")))
    return float(latest.get("value")) == value and str(latest.get("source_url")) == source_url


def _entered_at(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value)
    else:
        raise RuntimeError("existing PMI seed entered_at must be a datetime")
    if parsed.tzinfo is None:
        raise RuntimeError("existing PMI seed entered_at must include a timezone")
    return parsed.astimezone(UTC)


def extract_pmi_value(
    normalized_text: str,
    *,
    expected_observed_at: date,
    release_observed_at: date,
) -> float | None:
    """Extract a PMI value from a bounded headline context."""
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
        re.compile(
            rf"(?P<value>\d{{2}}\.\d)\s+(?:(?:in|for)\s+)?{month}\b",
            re.IGNORECASE,
        ),
        re.compile(
            rf"\b{month}(?:['’]s)?\s+(?P<value>\d{{2}}\.\d)\b",
            re.IGNORECASE,
        ),
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
                        r"\bfrom\s+(?P<value>\d{2}\.\d)\b",
                        sentence,
                        re.IGNORECASE,
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
        raise RuntimeError(
            f"conflicting PMI values for {expected_observed_at}: {sorted(candidates)}"
        )
    if not candidates:
        return None
    value = candidates.pop()
    if not 30 <= value <= 70:
        raise RuntimeError(f"PMI value outside plausible range: {value}")
    return value
