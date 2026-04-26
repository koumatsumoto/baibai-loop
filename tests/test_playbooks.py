from __future__ import annotations

import pytest

from baibai_loop.playbooks import playbook_short


def test_playbook_short_returns_registered_code() -> None:
    assert playbook_short("valuation-mean-reversion-v1") == "vmean"


def test_playbook_short_rejects_unknown_playbook() -> None:
    with pytest.raises(KeyError):
        playbook_short("unknown")
