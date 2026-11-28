"""One private receipt per verified NY session: raw SIP USD daily aggregates."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from hashlib import sha256
from typing import Annotated, Literal, Self
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from baibai_engine.storage.json import canonical_json
from baibai_engine.storage.objects.json import SavedObject, put_private, read_private
from baibai_engine.storage.objects.r2 import LakePublishError, ObjectStore

PREFIX = "l1/alpaca/sip-daily/v1/"
_MAX_DAY_BYTES = 8 * 1024 * 1024
_NY = ZoneInfo("America/New_York")

type Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class AlpacaSIPDayBar(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: UUID
    mic: str
    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    received_at: AwareDatetime
    receipt_sha256: Digest

    @property
    def instrument_id(self) -> str:
        return f"us:alpaca:{self.asset_id}"


class AlpacaSIPDay(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session: date
    closed_at: AwareDatetime
    scope_sha256: Digest
    bars: tuple[AlpacaSIPDayBar, ...]
    missing: tuple[UUID, ...]
    unverified: tuple[UUID, ...]
    feed: Literal["sip"] = "sip"
    adjustment: Literal["raw"] = "raw"
    price_unit: Literal["USD_per_share"] = "USD_per_share"

    @model_validator(mode="after")
    def one_session(self) -> Self:
        ids = (*[bar.asset_id for bar in self.bars], *self.missing, *self.unverified)
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("SIP day must account for each expected asset once")
        if self.closed_at.astimezone(_NY).date() != self.session or any(
            bar.received_at < self.closed_at for bar in self.bars
        ):
            raise ValueError("SIP day requires the verified NY session and post-close receipts")
        return self

    @property
    def expected(self) -> int:
        return len(self.bars) + len(self.missing) + len(self.unverified)


class SavedAlpacaSIPDay(BaseModel):
    model_config = ConfigDict(frozen=True)

    day: AlpacaSIPDay
    sha256: Digest


def _key(session: date) -> str:
    return PREFIX + session.isoformat() + ".json"


def _read_day(saved: SavedObject, *, session: date) -> SavedAlpacaSIPDay:
    day = AlpacaSIPDay.model_validate_json(saved.payload)
    if day.session != session:
        raise ValueError("saved SIP day differs from the requested NY session")
    return SavedAlpacaSIPDay(day=day, sha256=sha256(saved.payload).hexdigest())


def read_alpaca_sip_day(store: ObjectStore, *, session: date) -> SavedAlpacaSIPDay | None:
    saved = read_private(store, _key(session), max_bytes=_MAX_DAY_BYTES)
    return _read_day(saved, session=session) if saved is not None else None


def _merge(previous: AlpacaSIPDay, new: AlpacaSIPDay) -> AlpacaSIPDay:
    if (
        previous.scope_sha256 != new.scope_sha256
        or previous.closed_at != new.closed_at
        or previous.expected != new.expected
    ):
        raise ValueError("cannot replace a SIP session with a different close or asset scope")
    bars = {bar.asset_id: bar for bar in previous.bars}
    for bar in new.bars:
        old = bars.get(bar.asset_id)
        if old is None or bar.received_at > old.received_at:
            bars[bar.asset_id] = bar
    # Failure cannot erase a successful bar or convert a verified absence to unknown.
    missing = (set(previous.missing) | set(new.missing)) - bars.keys()
    unverified = (set(previous.unverified) | set(new.unverified)) - bars.keys() - missing
    return AlpacaSIPDay(
        session=new.session,
        closed_at=previous.closed_at,
        scope_sha256=previous.scope_sha256,
        bars=tuple(bars[key] for key in sorted(bars)),
        missing=tuple(sorted(missing)),
        unverified=tuple(sorted(unverified)),
    )


def save_alpaca_sip_day(store: ObjectStore, day: AlpacaSIPDay) -> SavedAlpacaSIPDay | None:
    """Install one day conditionally; a retry does not replace already proved coverage."""
    key = _key(day.session)
    saved = read_private(store, key, max_bytes=_MAX_DAY_BYTES)
    previous = _read_day(saved, session=day.session) if saved is not None else None
    if not day.bars and not day.missing:
        return previous  # No valid response in this attempt; there is no new coverage.
    selected = _merge(previous.day, day) if previous is not None else day
    if previous is not None and selected == previous.day:
        return previous
    payload = canonical_json(selected.model_dump(mode="json"))
    try:
        put_private(
            store,
            key,
            payload,
            previous_etag=None if saved is None else saved.etag,
            max_bytes=_MAX_DAY_BYTES,
        )
    except LakePublishError:
        # A committed request can lose its response. Do not overwrite a competitor.
        verified = read_private(store, key, max_bytes=_MAX_DAY_BYTES)
        if verified is None or verified.payload != payload:
            raise
    else:
        verified = read_private(store, key, max_bytes=_MAX_DAY_BYTES)
        if verified is None or verified.payload != payload:
            raise LakePublishError("SIP day write outcome is unconfirmed")
    return _read_day(verified, session=day.session)
