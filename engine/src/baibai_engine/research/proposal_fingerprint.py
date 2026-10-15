"""収集時刻を除き、実際に読んだ提案入力の事実を比較する。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any

from baibai_engine.research.proposal import ProposalInputs
from baibai_engine.storage.application.json import canonical_json


def _fact_value(value: object) -> Any:
    if isinstance(value, Decimal):
        # normalize()は現在のDecimal contextで丸め得るため、文字列だけを整える。
        if value == 0:
            return "0"
        number = format(value, "f")
        return number.rstrip("0").rstrip(".") if "." in number else number
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, dict):
        return {key: _fact_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_fact_value(item) for item in value]
    return value


def proposal_inputs_fingerprint(inputs: ProposalInputs) -> str:
    """checked_atだけを除き、Decimal同値/同instant/集合順に依らずhashする。"""

    payload = inputs.model_dump(mode="python", exclude={"checked_at"})
    for name, key in (
        ("positions", "ticker"),
        ("active_reservations", "reservation_id"),
        ("quotes", "ticker"),
        ("security_classification", "ticker"),
    ):
        payload[name] = sorted(payload[name], key=lambda row: row[key])
    for name in ("positions", "active_reservations"):
        for row in payload[name]:
            row["common_factors"] = sorted(row["common_factors"])
    payload["prior_buy_execution_references"] = sorted(payload["prior_buy_execution_references"])
    return sha256(canonical_json(_fact_value(payload)).encode()).hexdigest()
