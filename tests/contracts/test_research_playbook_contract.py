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
    "reinvestment-value": "reinvestment-value-research-v1",
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
            active[path.parent.name] = metadata

    assert set(active) == set(EXPECTED_ACTIVE_MAPPINGS)
    for approach_id, playbook_id in EXPECTED_ACTIVE_MAPPINGS.items():
        metadata = active[approach_id]
        assert metadata["research_playbook_id"] == playbook_id
        assert metadata["applies_to_valuation_approach_ids"] == [approach_id]
        assert "applies_to_evidence_pattern_ids" not in metadata


def test_research_skill_consumes_only_explicit_approach_applicability() -> None:
    text = RESEARCH_SKILL.read_text(encoding="utf-8")
    assert "applies_to_valuation_approach_ids" in text
    assert "implicitに" in text


def test_research_skill_continues_the_capital_allocation_session() -> None:
    text = RESEARCH_SKILL.read_text(encoding="utf-8")
    assert "activeな`capital-allocation` operation session" in text
    assert "別sessionを開始しない" in text
    assert "active な `research` operation session" not in text
