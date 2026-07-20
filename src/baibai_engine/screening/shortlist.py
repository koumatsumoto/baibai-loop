"""Canonical reviewed-shortlist judgment and publication service."""

from __future__ import annotations

import re
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.write import connect_rw, initialize_database


class ShortlistNarrative(BaseModel):
    """OP3 human-review judgment for a selected candidate.

    Screening は数値の由来を機械出力するが、なぜ深掘りに値するかという判断は
    ここへ人間/AI が固定する。selected 銘柄でだけ必須にし、reviewed shortlist を
    ephemeral な HTML narrative ではなく application DB の一次記録にする。
    """

    model_config = ConfigDict(extra="forbid")
    ploss: Literal["低", "中低", "中", "要精査", "高"]
    why: str = Field(min_length=1)
    temporary: str = Field(min_length=1)
    structural: str = Field(min_length=1)
    survive: str = Field(min_length=1)
    unlock: str = Field(min_length=1)
    counter: str = Field(min_length=1)
    research: str = Field(min_length=1)
    value: str = Field(min_length=1)
    prov: str = Field(min_length=1)
    sector_label: str | None = None


class ShortlistEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str = Field(pattern=r"^[0-9A-Z]{4}$")
    decision: Literal["selected", "rejected"]
    reason: str = Field(min_length=1)
    narrative: ShortlistNarrative | None = None

    @model_validator(mode="after")
    def validate_narrative_matches_decision(self) -> Self:
        if self.decision == "selected" and self.narrative is None:
            raise ValueError("selected shortlist entry must include an OP3 narrative")
        if self.decision == "rejected" and self.narrative is not None:
            raise ValueError("rejected shortlist entry must not include a narrative")
        return self


class ReviewedShortlist(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[2]
    kind: Literal["reviewed-shortlist"]
    shortlist_id: str
    selection_id: str = Field(min_length=1)
    run_revision_id: str = Field(min_length=1)
    as_of: date
    published_at: datetime
    profile: str = Field(min_length=1)
    macro_context_id: str | None = None
    entries: tuple[ShortlistEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_publication(self) -> Self:
        if not re.fullmatch(r"shortlist-\d{8}-[a-z0-9-]+", self.shortlist_id):
            raise ValueError("shortlist_id has an invalid format")
        if self.published_at.tzinfo is None:
            raise ValueError("published_at must include a timezone")
        tickers = [entry.ticker for entry in self.entries]
        if len(tickers) != len(set(tickers)):
            raise ValueError("shortlist ticker must be unique")
        if not any(entry.decision == "selected" for entry in self.entries):
            raise ValueError("shortlist must contain at least one selected entry")
        return self

    def payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class ShortlistConflictError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SelectionBinding:
    selection_id: str
    run_revision_id: str
    as_of: date
    profile: str
    macro_context_id: str | None
    candidate_tickers: frozenset[str]


class ReviewedShortlistService:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    def publish(
        self,
        shortlist: ReviewedShortlist,
        *,
        selection: SelectionBinding,
    ) -> ReviewedShortlist:
        expected = (
            selection.selection_id,
            selection.run_revision_id,
            selection.as_of,
            selection.profile,
            selection.macro_context_id,
        )
        actual = (
            shortlist.selection_id,
            shortlist.run_revision_id,
            shortlist.as_of,
            shortlist.profile,
            shortlist.macro_context_id,
        )
        if actual != expected:
            raise ShortlistConflictError("shortlist source selection metadata does not match")
        unknown = sorted(
            {entry.ticker for entry in shortlist.entries} - selection.candidate_tickers
        )
        if unknown:
            raise ShortlistConflictError(
                f"shortlist contains tickers outside source run: {', '.join(unknown)}"
            )
        initialize_database(self._db_path)
        payload = canonical_json(shortlist.payload())
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT payload FROM reviewed_shortlist WHERE shortlist_id = ?",
                    (shortlist.shortlist_id,),
                ).fetchone()
                if row is not None:
                    if str(row[0]) == payload:
                        connection.rollback()
                        return shortlist
                    raise ShortlistConflictError(
                        f"shortlist differs from existing publication: {shortlist.shortlist_id}"
                    )
                connection.execute(
                    """
                    INSERT INTO reviewed_shortlist (
                        shortlist_id, selection_id, run_revision_id, as_of, published_at, payload
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        shortlist.shortlist_id,
                        shortlist.selection_id,
                        shortlist.run_revision_id,
                        shortlist.as_of.isoformat(),
                        shortlist.published_at.isoformat(),
                        payload,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return shortlist


__all__ = [
    "ReviewedShortlist",
    "ReviewedShortlistService",
    "SelectionBinding",
    "ShortlistConflictError",
    "ShortlistEntry",
    "ShortlistNarrative",
]
