from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK_ROOT = ROOT / "method/research/playbooks"
RESEARCH_SKILL = ROOT / ".agents/skills/research/SKILL.md"

EXPECTED_ACTIVE_MAPPINGS = {
    "current-earnings-power": "current-earnings-power-research-v1",
    "normalized-earnings-power": "normalized-earnings-power-research-v1",
    "asset-value": "asset-value-research-v1",
    "reinvestment-value": "reinvestment-value-research-v2",
}


def _frontmatter(path: Path) -> dict[str, object]:
    parts = path.read_text(encoding="utf-8").split("---", maxsplit=2)
    payload = yaml.safe_load(parts[1])
    assert isinstance(payload, dict)
    return payload


def test_active_research_playbooks_map_explicitly_to_valuation_approaches() -> None:
    active: dict[str, dict[str, object]] = {}
    for path in PLAYBOOK_ROOT.glob("*/*.md"):
        metadata = _frontmatter(path)
        if metadata.get("status") == "active":
            assert path.parent.name not in active, f"multiple active playbooks: {path.parent}"
            active[path.parent.name] = metadata

    assert set(active) == set(EXPECTED_ACTIVE_MAPPINGS)
    for approach_id, playbook_id in EXPECTED_ACTIVE_MAPPINGS.items():
        metadata = active[approach_id]
        assert metadata["research_playbook_id"] == playbook_id
        assert metadata["applies_to_valuation_approach_ids"] == [approach_id]


def test_research_skill_completes_the_investment_proposal_session() -> None:
    text = RESEARCH_SKILL.read_text(encoding="utf-8")
    assert "analysis-operations.md#明示開始と固定対象" in text
    assert "--request" in text
    assert "review_positions" in text
    assert "--selection-file" in text
    assert "--operation-id" in text
    assert "record-unfinished" in text
    assert "operation complete <OPERATION_ID> --payload" in text
    assert "人間の採否・broker操作・実約定を完了条件へ補完しない" in text
