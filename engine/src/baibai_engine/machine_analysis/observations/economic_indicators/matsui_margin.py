"""One Matsui in-house long-margin percentage from retained current-page HTML."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from baibai_engine.data_collection.economic.documents import SourceDocument
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import SeriesDefinition
from baibai_engine.machine_analysis.observations.economic_indicators.observations import ObservationRecord

from baibai_engine.machine_analysis.observations.economic_indicators.validation import UnavailableObservationError, reading_observation

_TITLE = "信用残速報（松井証券内）"
_DAY = re.compile(r"(\d{1,2})/(\d{1,2})\s*\(([月火水木金土日])\)")
_PERCENT = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*[％%]")
_JST = ZoneInfo("Asia/Tokyo")


@dataclass(frozen=True)
class _Section:
    tokens: tuple[str, ...]
    notes: tuple[str, ...]


class _MarginSectionParser(HTMLParser):
    """Read only the publisher's margin box, never a market-wide neighbouring table."""

    def __init__(self) -> None:
        super().__init__()
        self.sections: list[_Section] = []
        self.depth = 0
        self.tokens: list[str] = []
        self.notes: list[str] = []
        self.note_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = (dict(attrs).get("class") or "").split()
        if tag == "div":
            if self.depth:
                self.depth += 1
            elif "market-brand-contents" in classes:
                self.depth = 1
                self.tokens = []
                self.notes = []
        if self.depth and tag == "p" and "market-brand-contents-text" in classes:
            self.note_parts = []

    def handle_data(self, data: str) -> None:
        if self.depth and (text := data.strip()):
            self.tokens.append(text)
            if self.note_parts is not None:
                self.note_parts.append(text)

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self.note_parts is not None:
            if note := " ".join(self.note_parts):
                self.notes.append(note)
            self.note_parts = None
        if tag == "div" and self.depth:
            self.depth -= 1
            if not self.depth:
                self.sections.append(_Section(tuple(self.tokens), tuple(self.notes)))


def _dated_section(document: SourceDocument) -> tuple[date, _Section]:
    if document.content_type != "text/html":
        raise ValueError("Matsui source is not HTML")
    receipt_day = document.fetched_at.astimezone(_JST).date()
    if (
        document.request_key != receipt_day.isoformat()
        or document.metadata.get("receipt_jst_date") != receipt_day.isoformat()
    ):
        raise ValueError("Matsui source receipt JST date does not match its request key")
    parser = _MarginSectionParser()
    parser.feed(document.payload.decode("utf-8-sig"))
    sections = [item for item in parser.sections if item.tokens and item.tokens[0] == _TITLE]
    if len(sections) != 1:
        raise ValueError("Matsui in-house margin section is missing or ambiguous")
    section = sections[0]
    match = _DAY.fullmatch(section.tokens[1]) if len(section.tokens) > 1 else None
    if match is None:
        raise ValueError("Matsui observation month/day/weekday is missing")
    month, day, weekday = match.groups()
    candidates = [
        candidate
        for offset in range(15)
        if (candidate := receipt_day - timedelta(days=offset)).month == int(month)
        and candidate.day == int(day)
        and "月火水木金土日"[candidate.weekday()] == weekday
    ]
    if len(candidates) != 1:
        raise ValueError("Matsui observation date has no unique match in receipt JST day -14..0")
    return candidates[0], section


def _long_percentage(section: _Section) -> float:
    tokens = section.tokens
    if tokens.count("買い残") != 1 or tokens.count("倍率") != 1:
        raise ValueError("Matsui long-margin row is missing or ambiguous")
    start, end = tokens.index("買い残"), tokens.index("倍率")
    row = tokens[start + 1 : end]
    if row.count("評価損益率") != 1:
        raise ValueError("Matsui long-margin evaluation label is missing or ambiguous")
    value_text = " ".join(row[row.index("評価損益率") + 1 :])
    match = _PERCENT.fullmatch(value_text)
    if match is None or not section.notes:
        raise ValueError("Matsui long-margin percentage or publisher note is missing")
    return float(match[1])


def read_matsui_margin_documents(
    series: SeriesDefinition,
    documents: Iterable[SourceDocument],
    *,
    start: date,
    end: date,
) -> tuple[ObservationRecord, ...]:
    if series.reading_key != "long_evaluation_pnl_percent" or series.unit != "percent":
        raise ValueError("unsupported Matsui margin reading definition")
    # read_documents already selected the latest saved receipt for each request
    # day. Choose the latest saved receipt per observation date before reading
    # its value, so a correction to missing cannot resurrect an older number.
    ordered = sorted(documents, key=lambda item: (item.fetched_at, item.request_key), reverse=True)
    selected: dict[date, tuple[SourceDocument, _Section]] = {}
    for index, document in enumerate(ordered):
        try:
            observed_at, section = _dated_section(document)
        except ValueError as error:
            if index == 0:
                raise UnavailableObservationError(f"Matsui latest saved source: {error}") from error
            continue  # Unreadable older dates are gaps, never reconstructed.
        selected.setdefault(observed_at, (document, section))

    # Observation range is independent of receipt time: keep later corrections
    # to an in-range observation, but do not parse values for later observations.
    in_range = sorted((day, entry) for day, entry in selected.items() if start <= day <= end)
    latest_in_range = max(
        (document for _, (document, _) in in_range),
        key=lambda item: (item.fetched_at, item.request_key),
        default=None,
    )
    values: list[ObservationRecord] = []
    for observed_at, (document, section) in in_range:
        try:
            observation = reading_observation(
                series,
                observed_at=observed_at,
                value=_long_percentage(section),
                vintage_at=document.fetched_at,
            )
        except ValueError as error:
            if document is latest_in_range:
                raise UnavailableObservationError(f"Matsui latest saved source: {error}") from error
            continue
        values.append(
            replace(
                observation,
                source_url=document.source_url,
                quality_flags=("year_inferred",),
                notes=(
                    (
                        f"原ラベル：{_TITLE} / 買い残 / 評価損益率。"
                        "母集団は松井証券内の信用買建。対象口座・建玉範囲・加重方法の詳細は未確認。"
                        "単位はpercent（原表示％）、公表値の符号を保持する。"
                    ),
                    (
                        f"観測日の年は受領JST日{document.request_key}の当日〜過去14日で"
                        f"原表示「{section.tokens[1]}」の月日・曜日と一致する一意候補から推定"
                        "（year_inferred）。14日は運用許容幅で、真の年や古い同月日再配信の排除を保証しない。"
                    ),
                    *section.notes,
                    (
                        "vintage_atは選択保存原本の受領時刻。公表時刻・初回既知時刻・最新取得時刻ではない。"
                        "known_atは適用しない。市場全体・追証人数・底値判定には使わず、"
                        "価格下落や信用残と独立した悪材料として加算しない。"
                    ),
                ),
            )
        )
    return tuple(values)
