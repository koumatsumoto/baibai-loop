"""Reject obsolete operational instructions from current documentation."""

from __future__ import annotations

import re
import sys
from pathlib import Path

_BEHAVIOR_LEGACY = re.compile(
    r"ai-value-bargain-selection|financial-pro-review|"
    r"durability_gate|execution lifecycle|"
    # Retired domain vocabulary (doctrine #vocabulary is the naming authority):
    # the judgment artifact is the thesis and the review input is the ranked set,
    # and the Research Gate output is the shortlist. The Git method tree is method/, so
    # reject any records/ path.
    r"decision.packet|packet.scaffold|packet.draft|--packet-id|research_packet|"
    r"audit.pool|--audit-top|reviewed.shortlist|cockpit|"
    # The single human-facing product name is Baibai Loop. `baibai-loop` (the
    # distribution) stays lowercase, so the hyphenated brand form is matched
    # case-sensitively while the rest of this pattern keeps IGNORECASE.
    r"Baibai App|(?-i:Baibai-Loop)|"
    # Retired macro context contract: the report declares no shelf life
    # (`valid_until`), core sections carry an economic connection rather than an
    # investment one, and there is one full-depth report instead of a
    # decision-grade / delta pair.
    r"valid_until|investment_connection|scenarios_connections|japan_specific|"
    r"fx_liquidity|decision-grade|delta 更新|delta更新|"
    # `(?<!/)` keeps retired path references (`records/`, `` `records/` ``) while
    # skipping `/records/` fragments inside external URLs.
    r"(?<!/)\brecords/|macro-dashboard|"
    # market lake の cutover 前語彙。L1 release が fetch 由来 15 table の正本なので、
    # legacy store と release を突き合わせる shadow 実行も、release が `shadow` または
    # `production` を選ぶ二状態の記述も現行の説明ではない。`ReleaseProfile` は
    # `Literal["production"]` で、`shadow` は不正値である。この pattern が見るのは
    # `.md` だけで、Tailwind の `shadow-*` や `drop-shadow` は `.tsx` にしかないため、
    # 結合を限定せず語そのものを拒否する — 「shadow または production」のように
    # 語の間に別の語が挟まる形も同じ退役語彙だからである。
    r"shadow|sqlite_authority|lake_authority|"
    # screening rules は dated revision で増え、現行 revision は
    # `rule_config.DEFAULT_RULES_PATH` が解決する。file 名の実値を書いた doc は次の改訂で
    # 存在しない path を「閾値の正本」として指すことになるので、revision を名指ししない。
    r"method/screening/rules/\d{4}-\d{2}-\d{2}",
    re.IGNORECASE,
)

_REPOSITORY_PATH_LEGACY = re.compile(
    r"(?:^|[\s`\"'(])src/baibai_(?:engine|app)|(?:^|[\s`\"'(])ui/|"
    r"(?:^|[\s`])cd\s+ui(?:/|\s|$)|(?:\.\./)+ui(?:/|\s|[\"'])|"
    r"reports/\d{4}-\d{2}-\d{2}-|"
    r"cloud/worker|tools/cloud|data/(?:app|screening|indicators)|"
    # 固定 release を SQLite へ実体化するのは `lake hydrate` だけになった。projection
    # subsystem とその診断は削除済みなので、その module・CLI・成果物・metadata を指す
    # 参照は存在しない path を現行手順として提示することになる。`projection` 単体は
    # doctrine の表示物にも当たるため、退役した結合だけを拒否する。
    r"lake_shadow|verify_lake_release_parity|benchmark_lake_projection|"
    r"build_projection|projection\.sqlite|projection_meta|projection_fingerprint|"
    r"projection_indexes|lake projection build|"
    r"method/(?:macro-panel|screening-rules|macro-reading|playbooks)",
    re.IGNORECASE,
)

_DOMAIN_IDENTIFIER_LEGACY = re.compile(
    r"\bOP3\b|\blenses?\b|AssessmentLane|LaneDisposition|LaneMachineValues|"
    r"candidate_lenses|durability_lens|durability_gate|screening_playbooks?|selection_playbook|"
    r"research_selection_playbook_order|opportunity_lane_id|"
    r"attention_policy_(?:id|hash|parameters)|selection_policy_(?:id|hash)|"
    r"publication_kind|source_selection_id|position_intent|edinet_buyback_reports|"
    r"buyback_authorization|buyback_status_|buyback_remaining_|"
    r"refresh-buyback-reports|measure_buyback_authorization|"
    r"screening shortlist preflight|baibai-engine proposal|baibai_engine/proposals|"
    r"profile_overrides|default_profile|build_selection_sweep_payload|recommended_rank|"
    r"application_git_commit|selection_entry|supply_demand_liquidity|"
    r"measure_supply_context|history-backfill|"
    r"(?m:^profile:\s*[\"']<selection profile>[\"'])|"
    r"jquants_earnings_calendar|jquants\.earnings_calendar|deep_discount_bps|"
    r"\bReview Set\b|\bSelection Policy\b",
    re.IGNORECASE,
)

# Documentation surfaces that state current behaviour. reports/ holds dated
# measurement records that intentionally keep the vocabulary of their time, so it
# is excluded; everything an agent reads as current instruction is scanned.
_SCAN_DIRECTORIES = (
    "docs",
    ".agents/skills",
    "engine",
    "web",
    "batch",
    "tools",
    "method",
    "stores",
    ".github",
)
_ROOT_FILES = ("README.md", "AGENTS.md", "CLAUDE.md")
_PATH_ROOT_FILES = (
    *_ROOT_FILES,
    "pyproject.toml",
    ".pre-commit-config.yaml",
    ".gitignore",
    ".env.sample",
)
_CURRENT_SUFFIXES = {".md", ".py", ".sh", ".yaml", ".yml", ".json", ".toml", ".ts", ".tsx"}
_IGNORED_PARTS = {"node_modules", ".playwright-cli", "__pycache__"}
_PATH_PATTERN_OWNERS = {
    Path("engine/src/baibai_engine/foundation/repository_layout.py"),
    Path("tools/quality/drift/check_legacy_semantics.py"),
}
_DOMAIN_IDENTIFIER_ADAPTERS = {
    # One-shot operator cutover reads the retired payload and emits only the current schema.
    Path("tools/migrations/cutover_application_v17.py"),
    Path("tools/migrations/cutover_market_v25.py"),
    Path("tools/migrations/publish_market_lake_v25.py"),
    Path("tools/migrations/cutover_runs_v4.py"),
}


def check(root: Path) -> list[str]:
    behavior_paths = [root / name for name in _ROOT_FILES]
    current_paths = [root / name for name in _PATH_ROOT_FILES if (root / name).is_file()]
    for directory_name in _SCAN_DIRECTORIES:
        directory = root / directory_name
        if directory.is_dir():
            paths = [
                path
                for path in sorted(directory.rglob("*"))
                if path.suffix in _CURRENT_SUFFIXES
                and not (_IGNORED_PARTS & set(path.parts))
                # stores/ contains runtime mirrors and immutable manifests alongside
                # its tracked README files. Generated JSON is evidence, not current
                # source, and may legitimately retain the vocabulary of its generation.
                and (directory_name != "stores" or path.suffix == ".md")
            ]
            current_paths.extend(paths)
            behavior_paths.extend(path for path in paths if path.suffix == ".md")
    errors: list[str] = []
    for path in behavior_paths:
        if not path.is_file():
            continue
        if match := _BEHAVIOR_LEGACY.search(path.read_text(encoding="utf-8")):
            errors.append(
                f"{path.relative_to(root)}: obsolete operation instruction {match.group(0)!r}"
            )
    for path in current_paths:
        relative_path = path.relative_to(root)
        text = path.read_text(encoding="utf-8")
        if relative_path not in _PATH_PATTERN_OWNERS | _DOMAIN_IDENTIFIER_ADAPTERS and (
            match := _DOMAIN_IDENTIFIER_LEGACY.search(text)
        ):
            errors.append(f"{relative_path}: retired domain identifier {match.group(0)!r}")
        if relative_path in _PATH_PATTERN_OWNERS:
            continue
        if match := _REPOSITORY_PATH_LEGACY.search(text):
            errors.append(f"{relative_path}: obsolete repository path {match.group(0)!r}")
    return errors


def main() -> int:
    errors = check(Path.cwd())
    print("\n".join(errors), file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
