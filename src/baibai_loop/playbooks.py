from __future__ import annotations

from collections.abc import Mapping

PLAYBOOK_SHORT_MAP: Mapping[str, str] = {
    "valuation-mean-reversion-v1": "vmean",
    "valuation-catalyst-confirmation-v1": "vcatalyst",
}


def playbook_short(playbook: str) -> str:
    return PLAYBOOK_SHORT_MAP[playbook]
