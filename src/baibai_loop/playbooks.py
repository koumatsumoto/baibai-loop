from __future__ import annotations

from collections.abc import Mapping

PLAYBOOK_SHORT_MAP: Mapping[str, str] = {
    "valuation-reversion": "vreversion",
    "cash-rich-asset-discount": "cashrich",
    "cashflow-yield-discount": "cfyield",
    "sales-discount-growth": "salesgrowth",
}


def playbook_short(playbook: str) -> str:
    return PLAYBOOK_SHORT_MAP[playbook]
