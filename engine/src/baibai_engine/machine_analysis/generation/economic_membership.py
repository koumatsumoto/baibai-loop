"""Short fixed receipt sets that fit in the 64-character slot of their old block digest."""

from __future__ import annotations

import base64
import calendar
import hashlib
import struct
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from baibai_engine.machine_analysis.generation.economic_inputs import _Receipt

type Member = tuple[str, tuple[str, ...], bool, str | None, str | None]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_CALENDAR = struct.Struct(">HHH")  # covered offsets, count and seven two-bit weekday defaults
_CLOCK = struct.Struct(">q")


def _clock(text: str) -> int:
    value = datetime.fromisoformat(text)
    if value.tzinfo != UTC or value.isoformat(timespec="microseconds") != text:
        raise ValueError("noncanonical native clock")
    return (value - _EPOCH) // timedelta(microseconds=1)


def _clock_text(value: int) -> str:
    return (_EPOCH + timedelta(microseconds=value)).isoformat(timespec="microseconds")


def inline_year(name: str, rows: dict[str, list[_Receipt]]) -> str | None:
    """Admission depends only on this original block, never on other blocks or a prior run."""
    if rows["documents"] or len(rows["records"]) < 32:
        return None
    source, _, year = name.rpartition("/")
    if not (len(year) == 4 and year.isascii() and year.isdecimal() and year != "0000"):
        return None
    first = date(int(year), 1, 1)
    states: dict[tuple[str, bool, str | None], int] = {}
    days = [0] * (365 + int(calendar.isleap(first.year)))
    start = previous = -1
    for row in rows["records"]:
        source_id, day, received = row.key
        try:
            observed = date.fromisoformat(day)
        except ValueError:
            return None
        offset = (observed - first).days
        if (
            source_id != source
            or observed.year != first.year
            or observed.isoformat() != day
            or offset <= previous
            or (row.confirmed and row.checked_at is not None)
        ):
            return None
        if start == -1:
            start = offset
        previous = offset
        state = (received, row.confirmed, row.checked_at)
        days[offset] = states.setdefault(state, len(states) + 1)
        if len(states) > 2:
            return None

    # Weekly, monthly and quarterly confirmations often differ from a weekly
    # calendar in only a few places. Encode the exact exceptions; do not infer days.
    span = days[start : previous + 1]
    pattern = [max(range(len(states) + 1), key=span[i::7].count) for i in range(7)]
    code = sum(value << (2 * i) for i, value in enumerate(pattern))
    wire = bytearray(_CALENDAR.pack(start, previous, (len(states) << 14) | code))
    try:
        for received, confirmed, checked in states:
            wire.append(2 if checked is not None else int(confirmed))
            wire.extend(_CLOCK.pack(_clock(received)))
            if checked is not None:
                wire.extend(_CLOCK.pack(_clock(checked)))
    except ValueError:
        return None
    last = -1
    for pos, value in enumerate(span):
        if value != pattern[pos % 7]:
            number = 3 * (pos - last) + value
            if number >= 128:
                wire.append(128 | (number >> 8))
            wire.append(number & 255)
            last = pos
    text = "~" + base64.b64encode(wire).decode("ascii").rstrip("=")
    return text if len(text) <= 64 else None


def year_members(name: str, text: str) -> Iterator[Member]:
    try:
        yield from _year_members(name, text)
    except (IndexError, OverflowError, struct.error, ValueError) as exc:
        raise ValueError("invalid inline economic membership") from exc


def _year_members(name: str, text: str) -> Iterator[Member]:
    source, _, year = name.rpartition("/")
    first = date(int(year), 1, 1)
    if not text.startswith("~") or len(text) > 64:
        raise ValueError("invalid inline economic membership")
    encoded = text[1:]
    raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
    start, end, packed = _CALENDAR.unpack_from(raw)
    count = packed >> 14
    pattern = [(packed >> (2 * i)) & 3 for i in range(7)]
    if (
        count not in (1, 2)
        or not 0 <= start <= end < 365 + int(calendar.isleap(first.year))
        or max(pattern) > count
    ):
        raise ValueError("invalid inline economic calendar")
    offset = _CALENDAR.size
    states = []
    for _ in range(count):
        flag = raw[offset]
        if flag not in (0, 1, 2):
            raise ValueError("invalid inline economic state")
        fetched = _clock_text(_CLOCK.unpack_from(raw, offset + 1)[0])
        offset += 1 + _CLOCK.size
        checked = None
        if flag == 2:
            checked = _clock_text(_CLOCK.unpack_from(raw, offset)[0])
            offset += _CLOCK.size
        states.append((fetched, flag == 1, checked))
    days = [pattern[i % 7] for i in range(end - start + 1)]
    pos = -1
    while offset < len(raw):
        number = raw[offset]
        offset += 1
        if number >= 128:
            number = ((number & 127) << 8) | raw[offset]
            offset += 1
        step, value = divmod(number, 3)
        pos += step
        if not step or pos >= len(days) or value > count:
            raise ValueError("invalid inline economic day")
        days[pos] = value
    for pos, value in enumerate(days):
        if value:
            fetched, confirmed, checked = states[value - 1]
            yield (
                "records",
                (source, (first + timedelta(days=start + pos)).isoformat(), fetched),
                confirmed,
                checked,
                None,
            )


def hybrid_root(legacy: dict[str, Any], selections: dict[str, str], proof: str) -> dict[str, Any]:
    # Two binary SHA-256 values in one base64 field cost less than the old format
    # tag and hex recipe digest together. Each replaced digest also costs at most 64.
    root = dict(legacy)
    del root["format"]
    recipe = root.pop("recipe_sha256")
    return {
        **root,
        "v": 3,
        "h": base64.b64encode(bytes.fromhex(recipe + proof)).decode("ascii"),
        "months": selections,
    }


def unpack_root(root: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str], str]:
    value = dict(root)
    if value.pop("v", None) != 3:
        raise ValueError("invalid hybrid economic selection")
    encoded = value.pop("h", None)
    if not isinstance(encoded, str):
        raise ValueError("invalid hybrid economic selection proof")
    digests = base64.b64decode(encoded, validate=True)
    if len(digests) != 2 * hashlib.sha256().digest_size:
        raise ValueError("invalid hybrid economic selection proof")
    members = value.pop("months", None)
    if not isinstance(members, dict) or not all(isinstance(v, str) for v in members.values()):
        raise ValueError("invalid hybrid economic selection")
    periods = {k: v for k, v in members.items() if not v.startswith("~")}
    inline = {k: v for k, v in members.items() if v.startswith("~")}
    return (
        {
            **value,
            "format": "economic-selection-v2",
            "recipe_sha256": digests[:32].hex(),
            "months": periods,
        },
        inline,
        digests[32:].hex(),
    )
