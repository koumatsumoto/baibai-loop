"""Verify the canonical repository skill inventory and Claude compatibility links."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

EXPECTED = frozenset(
    {
        "shortlist",
        "research",
        "holding-review",
        "ledger-record",
        "macro-context",
        "ops-maintenance",
    }
)
OLD = frozenset(
    {
        "ai-value-bargain-selection",
        "ir-research",
        "financial-pro-review",
        "decision-cycle",
        "macro-analysis",
        "improvement-loop",
        "macro-world-model",
    }
)


def check(root: Path) -> list[str]:
    errors: list[str] = []
    canonical_root = root / ".agents" / "skills"
    claude_root = root / ".claude" / "skills"
    canonical = _entry_names(canonical_root, directories_only=True)
    claude = _entry_names(claude_root, directories_only=False)
    if canonical != EXPECTED:
        errors.append(
            f".agents/skills: expected exact inventory {sorted(EXPECTED)}, got {sorted(canonical)}"
        )
    if claude != EXPECTED:
        errors.append(
            f".claude/skills: expected exact inventory {sorted(EXPECTED)}, got {sorted(claude)}"
        )
    if canonical_root.is_dir():
        for path in canonical_root.rglob("*"):
            relative = path.relative_to(root)
            if path.is_symlink():
                errors.append(f"{relative}: canonical skill tree must not contain symlinks")
                continue
            if not (path.is_file() or path.is_dir()):
                errors.append(f"{relative}: unsupported canonical skill file type")
            try:
                path.resolve().relative_to(canonical_root.resolve())
            except ValueError:
                errors.append(f"{relative}: canonical skill path escapes .agents/skills")
    for name in sorted(EXPECTED):
        canonical_path = canonical_root / name
        claude_path = claude_root / name
        for relative in (Path("SKILL.md"), Path("agents/openai.yaml")):
            if not canonical_path.joinpath(relative).is_file():
                errors.append(f".agents/skills/{name}/{relative}: missing canonical skill file")
        metadata_path = canonical_path / "agents" / "openai.yaml"
        skill_path = canonical_path / "SKILL.md"
        if skill_path.is_file() and not skill_path.is_symlink():
            errors.extend(_check_frontmatter(skill_path, root=root, expected_name=name))
        if metadata_path.is_file() and not metadata_path.is_symlink():
            try:
                metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
                default_prompt = metadata["interface"]["default_prompt"]
            except (KeyError, TypeError, yaml.YAMLError) as error:
                errors.append(
                    f".agents/skills/{name}/agents/openai.yaml: invalid metadata: {error}"
                )
            else:
                interface = metadata.get("interface") if isinstance(metadata, dict) else None
                policy = metadata.get("policy") if isinstance(metadata, dict) else None
                if not isinstance(interface, dict) or not all(
                    isinstance(interface.get(key), str) and interface[key].strip()
                    for key in ("display_name", "short_description", "default_prompt")
                ):
                    errors.append(
                        f".agents/skills/{name}/agents/openai.yaml: interface strings are required"
                    )
                if not isinstance(policy, dict) or not isinstance(
                    policy.get("allow_implicit_invocation"), bool
                ):
                    errors.append(
                        f".agents/skills/{name}/agents/openai.yaml: "
                        "policy.allow_implicit_invocation must be boolean"
                    )
                if not isinstance(default_prompt, str) or f"${name}" not in default_prompt:
                    errors.append(
                        f".agents/skills/{name}/agents/openai.yaml: "
                        f"default_prompt must mention ${name}"
                    )
        if not claude_path.is_symlink():
            errors.append(f".claude/skills/{name}: must be a relative symlink")
            continue
        target = str(claude_path.readlink())
        expected_target = f"../../.agents/skills/{name}"
        if target != expected_target:
            errors.append(
                f".claude/skills/{name}: expected symlink {expected_target}, got {target}"
            )
        if claude_path.resolve() != canonical_path.resolve():
            errors.append(f".claude/skills/{name}: symlink does not resolve to canonical skill")
    for name in sorted(OLD):
        if canonical_root.joinpath(name).exists() or claude_root.joinpath(name).exists():
            errors.append(f"obsolete standalone skill remains: {name}")
    return errors


def _check_frontmatter(path: Path, *, root: Path, expected_name: str) -> list[str]:
    relative = path.relative_to(root)
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return [f"{relative}: missing YAML frontmatter"]
    frontmatter, _body = text[4:].split("\n---\n", maxsplit=1)
    try:
        metadata = yaml.safe_load(frontmatter)
    except yaml.YAMLError as error:
        return [f"{relative}: invalid YAML frontmatter: {error}"]
    if not isinstance(metadata, dict):
        return [f"{relative}: YAML frontmatter must be a mapping"]
    errors: list[str] = []
    if metadata.get("name") != expected_name:
        errors.append(f"{relative}: frontmatter name must equal {expected_name}")
    if not isinstance(metadata.get("description"), str) or not metadata["description"].strip():
        errors.append(f"{relative}: frontmatter description must be a non-empty string")
    return errors


def _entry_names(path: Path, *, directories_only: bool) -> frozenset[str]:
    if not path.is_dir():
        return frozenset()
    return frozenset(
        item.name
        for item in path.iterdir()
        if ((item.is_dir() and not item.is_symlink()) if directories_only else True)
    )


def main() -> int:
    errors = check(Path.cwd())
    print("\n".join(errors), file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
