from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, cast

import yaml

from baibai_loop.coerce import optional_float, parse_iso_date

PriceBasis = Literal["close_unadjusted", "adjusted_close", "intraday_last"]
TrackingHorizon = Literal["plus_15bd", "plus_30bd"]

_PRICE_BASES = {"close_unadjusted", "adjusted_close", "intraday_last"}
_TRACKING_HORIZONS = {"plus_15bd", "plus_30bd"}


@dataclass(frozen=True, slots=True)
class PriceObservation:
    decision_event_id: str
    ticker: str
    target_date: date
    resolved_trade_date: date
    price: float
    price_basis: PriceBasis
    source_name: str
    source_url: str
    fetched_at: str
    corporate_action_checked: bool
    same_basis_group_id: str
    provisional: bool
    ref_path: str
    tracking_horizon: TrackingHorizon

    def source_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "source_kind": "fallback",
            "ref_path": self.ref_path,
            "decision_event_id": self.decision_event_id,
            "target_date": self.target_date.isoformat(),
            "resolved_trade_date": self.resolved_trade_date.isoformat(),
            "price_basis": self.price_basis,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
            "corporate_action_checked": self.corporate_action_checked,
            "same_basis_group_id": self.same_basis_group_id,
            "provisional": self.provisional,
        }
        payload["tracking_horizon"] = self.tracking_horizon
        return payload


def load_fallback_price_observations(
    root: Path,
) -> tuple[tuple[PriceObservation, ...], tuple[str, ...]]:
    market_data_root = root / "records/_market-data"
    if not market_data_root.exists():
        return (), ()
    observations: list[PriceObservation] = []
    warnings: list[str] = []
    for path in sorted(market_data_root.rglob("*.yaml")) + sorted(market_data_root.rglob("*.yml")):
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            warnings.append(f"skip fallback market data {path}: {exc}")
            continue
        if not isinstance(document, Mapping):
            warnings.append(f"skip fallback market data {path}: root must be a mapping")
            continue
        ref_path = path.relative_to(root).as_posix()
        observations.extend(_observations_from_document(document, ref_path, warnings))
    return tuple(observations), tuple(warnings)


def _observations_from_document(
    document: Mapping[object, object],
    ref_path: str,
    warnings: list[str],
) -> list[PriceObservation]:
    raw_observations = document.get("observations")
    if isinstance(raw_observations, list):
        parsed: list[PriceObservation] = []
        for index, item in enumerate(raw_observations):
            if not isinstance(item, Mapping):
                warnings.append(f"skip {ref_path} observations[{index}]: must be a mapping")
                continue
            observation = _parse_observation(item, ref_path, f"observations[{index}]", warnings)
            if observation is not None:
                parsed.append(observation)
        return parsed

    return []


def _parse_observation(
    item: Mapping[object, object],
    ref_path: str,
    location: str,
    warnings: list[str],
) -> PriceObservation | None:
    ticker = item.get("ticker")
    decision_event_id = item.get("decision_event_id")
    target_date = parse_iso_date(item.get("target_date"))
    resolved_trade_date = parse_iso_date(item.get("resolved_trade_date"))
    price = optional_float(item.get("price"))
    price_basis = _price_basis(item.get("price_basis"))
    source_name = item.get("source_name")
    source_url = item.get("source_url")
    fetched_at = item.get("fetched_at")
    same_basis_group_id = item.get("same_basis_group_id")
    corporate_action_checked = item.get("corporate_action_checked")
    provisional = item.get("provisional")
    tracking_horizon = item.get("tracking_horizon")
    if (
        not isinstance(ticker, str)
        or not isinstance(decision_event_id, str)
        or target_date is None
        or resolved_trade_date is None
        or price is None
        or price_basis is None
        or not isinstance(source_name, str)
        or not isinstance(source_url, str)
        or not isinstance(fetched_at, str)
        or not isinstance(same_basis_group_id, str)
        or not isinstance(corporate_action_checked, bool)
        or not isinstance(provisional, bool)
    ):
        warnings.append(f"skip {ref_path} {location}: missing or invalid observation fields")
        return None
    if tracking_horizon not in _TRACKING_HORIZONS:
        warnings.append(f"skip {ref_path} {location}: invalid tracking_horizon")
        return None
    effective_provisional = (
        provisional or not corporate_action_checked or price_basis == "intraday_last"
    )
    return PriceObservation(
        decision_event_id=decision_event_id,
        ticker=ticker,
        target_date=target_date,
        resolved_trade_date=resolved_trade_date,
        price=price,
        price_basis=price_basis,
        source_name=source_name,
        source_url=source_url,
        fetched_at=fetched_at,
        corporate_action_checked=corporate_action_checked,
        same_basis_group_id=same_basis_group_id,
        provisional=effective_provisional,
        ref_path=ref_path,
        tracking_horizon=cast(TrackingHorizon, tracking_horizon),
    )


def _price_basis(value: object) -> PriceBasis | None:
    if isinstance(value, str) and value in _PRICE_BASES:
        return value  # type: ignore[return-value]
    return None
