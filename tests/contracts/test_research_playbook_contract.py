from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK_ROOT = ROOT / "method/research/playbooks"
RESEARCH_SKILL = ROOT / ".agents/skills/research/SKILL.md"

EXPECTED_ACTIVE_MAPPINGS = {
    "cash-rich-asset-discount": (
        "cash-rich-asset-discount-research-v1",
        "cash-rich-asset-discount",
    ),
    "cashflow-yield-discount": (
        "cashflow-yield-discount-research-v1",
        "cashflow-yield-discount",
    ),
    "sales-discount-growth": (
        "sales-discount-growth-research-v1",
        "sales-discount-growth",
    ),
    "valuation-reversion": (
        "valuation-reversion-research-v1",
        "valuation-reversion",
    ),
}


def _frontmatter(path: Path) -> dict[str, object]:
    parts = path.read_text(encoding="utf-8").split("---", maxsplit=2)
    payload = yaml.safe_load(parts[1])
    assert isinstance(payload, dict)
    return payload


def test_active_research_playbooks_have_explicit_versioned_applicability() -> None:
    for directory_slug, (playbook_id, evidence_pattern_id) in EXPECTED_ACTIVE_MAPPINGS.items():
        versions = sorted((PLAYBOOK_ROOT / directory_slug).glob("*.md"))
        active = [path for path in versions if _frontmatter(path).get("status") == "active"]
        assert len(active) == 1
        metadata = _frontmatter(active[0])
        assert metadata["research_playbook_id"] == playbook_id
        assert "playbook_id" not in metadata
        assert metadata["applies_to_opportunity_lane_ids"] == []
        assert metadata["applies_to_evidence_pattern_ids"] == [evidence_pattern_id]


def test_research_skill_consumes_only_explicit_playbook_applicability() -> None:
    text = RESEARCH_SKILL.read_text(encoding="utf-8")
    assert "applies_to_opportunity_lane_ids" in text
    assert "applies_to_evidence_pattern_ids" in text
    assert "implicitに" in text
