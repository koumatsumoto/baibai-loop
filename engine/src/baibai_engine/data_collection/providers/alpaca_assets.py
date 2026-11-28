"""Decode Alpaca Paper's active US equity asset listing, independent of persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal
from unicodedata import category
from uuid import UUID

from baibai_engine.data_collection.providers.alpaca import decode_alpaca_json

ASSETS_URL = "https://paper-api.alpaca.markets/v2/assets?status=active&asset_class=us_equity"
ASSETS_BODY_MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class AlpacaAsset:
    """Native identity and availability without a derived product classification."""

    id: UUID
    symbol: str
    exchange: str
    name: str | None
    status: Literal["active"]
    tradable: bool
    attributes: tuple[str, ...]


def _display_name(raw: object) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        # The display label is optional; the receipt still retains the original bytes.
        name = raw if raw.isprintable() else "".join(char for char in raw if category(char) != "Cf")
        visible = bool(name.strip())
        if (visible or name != raw) and all(
            char.isprintable() or category(char) == "Zs" for char in name
        ):
            return name if visible else None
    raise ValueError("Alpaca Assets contains an invalid asset name")


def parse_alpaca_us_assets(body: bytes) -> tuple[AlpacaAsset, ...]:
    """Normalize the named active-equity contract without inventing market or product types."""
    if type(body) is not bytes or not 0 < len(body) <= ASSETS_BODY_MAX_BYTES:
        raise ValueError("Alpaca Assets requires a bounded original response")
    try:
        rows = decode_alpaca_json(body)
    except (ValueError, ArithmeticError, RecursionError):
        raise ValueError("Alpaca Assets response is not valid JSON") from None
    # Empty is not evidence that the entire current US universe disappeared.
    if not isinstance(rows, list) or not rows:
        raise ValueError("Alpaca Assets requires a nonempty active US equity array")

    assets: list[AlpacaAsset] = []
    seen: set[UUID] = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or row.get("class") != "us_equity"
            or row.get("status") != "active"
            or not isinstance(symbol := row.get("symbol"), str)
            or not symbol.isascii()
            or not symbol.isprintable()
            or not symbol.strip()
            or not isinstance(exchange := row.get("exchange"), str)
            or not exchange.isprintable()
            or not exchange.strip()
            or type(tradable := row.get("tradable")) is not bool
            or not isinstance(raw_id := row.get("id"), str)
        ):
            raise ValueError("Alpaca Assets contains an invalid active US equity row")
        name = _display_name(row.get("name"))
        try:
            asset_id = UUID(raw_id)
        except ValueError:
            raise ValueError("Alpaca Assets contains an invalid asset UUID") from None
        if str(asset_id) != raw_id.lower():
            raise ValueError("Alpaca Assets contains an invalid asset UUID")
        if asset_id in seen:
            raise ValueError("Alpaca Assets repeats an asset UUID")
        seen.add(asset_id)

        attributes = row.get("attributes")
        if attributes is None:
            attributes = []
        if not isinstance(attributes, list) or any(
            not isinstance(value, str) or not value.strip() or not value.isprintable()
            for value in attributes
        ):
            raise ValueError("Alpaca Assets contains invalid attributes")
        # Keep provider symbols outside the narrower daily-bar label grammar. Eligibility
        # for a price request is a separate decision in the consumer, not a dropped asset.
        assets.append(
            AlpacaAsset(asset_id, symbol, exchange, name, "active", tradable, tuple(attributes))
        )
    return tuple(assets)


@dataclass(frozen=True, slots=True)
class AlpacaAssetsReceipt:
    """One complete Paper response; caller owns its storage and downstream use."""

    received_at: datetime
    body: bytes = field(repr=False)
    body_sha256: str = field(init=False)
    assets: tuple[AlpacaAsset, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.received_at) is not datetime or self.received_at.utcoffset() is None:
            raise ValueError("Alpaca Assets requires a timezone-aware receipt")
        assets = parse_alpaca_us_assets(self.body)
        object.__setattr__(self, "received_at", self.received_at.astimezone(UTC))
        object.__setattr__(self, "body_sha256", sha256(self.body).hexdigest())
        object.__setattr__(self, "assets", assets)
