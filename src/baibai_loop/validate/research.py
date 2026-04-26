"""Validate research markdown front matter and playbook-specific body sections."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from .errors import ValidationFinding
from .playbook_schema import (
    PlaybookSchemaError,
    discover_playbook_schemas,
    load_playbook_schema,
    validate_research_body,
)

KNOWN_MACRO_GATES: tuple[str, ...] = ("tailwind", "neutral", "headwind")
KNOWN_DECISIONS: tuple[str, ...] = ("accepted", "skipped", "pending")
MEAN_REVERSION_PLAYBOOK = "valuation-mean-reversion-v1"
REQUIRED_FRONT_MATTER: tuple[str, ...] = (
    "ticker",
    "name",
    "playbook",
    "decision",
    "market_cap_oku",
    "sector_33",
    "screened_ref",
    "view_ref",
    "brief_refs",
    "ai-draft",
    "published_at",
    "tradable_at",
    "macro_gate",
    "position_size_oku",
    "valuation",
)
_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_TICKER_PATTERN = re.compile(r"^[0-9A-Z]{4}$")


def validate_research_file(
    path: Path,
    *,
    playbooks_root: Path | None = None,
    known_playbooks: frozenset[str] | None = None,
) -> list[ValidationFinding]:
    loaded = _load_research_document(path)
    if isinstance(loaded, list):
        return loaded
    front_matter, body = loaded
    playbook_root = playbooks_root or _default_playbook_root()
    # CLI は run_validation で 1 回だけ discover してくる。単独呼び出し時のため
    # フォールバックとして自前 discover を残す。
    if known_playbooks is None:
        known_playbooks = frozenset(discover_playbook_schemas(playbook_root))
    findings: list[ValidationFinding] = []
    findings.extend(_validate_front_matter(path, front_matter, known_playbooks))
    playbook = front_matter.get("playbook")
    if isinstance(playbook, str) and playbook in known_playbooks:
        try:
            schema = load_playbook_schema(playbook_root, playbook)
        except FileNotFoundError as exc:
            # discover_playbook_schemas との競合状態 (validate 実行中に schema YAML
            # が消えた等) で発生しうる。uncaught で die せず error finding に変換する。
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.missing-playbook-schema",
                    message=str(exc),
                    location=f"playbook:{playbook}",
                )
            )
        except PlaybookSchemaError as exc:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.invalid-playbook-schema",
                    message=str(exc),
                    location=f"playbook:{playbook}",
                )
            )
        else:
            findings.extend(validate_research_body(path, body, schema))
    return findings


def parse_research_front_matter(path: Path) -> dict[str, object] | None:
    loaded = _load_research_document(path)
    if isinstance(loaded, list):
        return None
    front_matter, _body = loaded
    return front_matter


def validate_research_collection(
    paths_with_front_matter: Sequence[tuple[Path, Mapping[str, object]]],
) -> list[ValidationFinding]:
    accepted_by_sector: dict[str, list[Path]] = {}
    for path, front_matter in paths_with_front_matter:
        if front_matter.get("decision") != "accepted":
            continue
        sector = front_matter.get("sector_33")
        if isinstance(sector, str) and sector.strip():
            accepted_by_sector.setdefault(sector, []).append(path)

    findings: list[ValidationFinding] = []
    for sector, paths in sorted(accepted_by_sector.items()):
        if len(paths) < 3:
            continue
        for path in paths:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    target=path,
                    code="research.sector-concentration",
                    message=(
                        f"3+ accepted research packets share sector_33={sector}; "
                        "review for concentration risk"
                    ),
                    location="sector_33",
                )
            )
    return findings


def _load_research_document(
    path: Path,
) -> tuple[dict[str, object], str] | list[ValidationFinding]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.io",
                message=f"failed to read file: {exc}",
            )
        ]
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.no-front-matter",
                message="research markdown must start with `---` YAML front matter",
            )
        ]
    try:
        front_matter = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.invalid-yaml",
                message=f"front matter YAML parse failed: {exc}",
            )
        ]
    if not isinstance(front_matter, dict):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.front-matter-non-mapping",
                message="research front matter must be a mapping",
            )
        ]
    body = match.group(2)
    return front_matter, body


def discover_research_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.md") if p.is_file())


def _default_playbook_root() -> Path:
    return Path(__file__).resolve().parents[3] / "playbooks"


def _validate_front_matter(
    path: Path,
    front_matter: dict[str, object],
    known_playbooks: frozenset[str],
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for field in REQUIRED_FRONT_MATTER:
        if field not in front_matter:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.missing-field",
                    message=f"required front matter field missing: {field}",
                    location=field,
                )
            )
    ticker = front_matter.get("ticker")
    if isinstance(ticker, str) and not _TICKER_PATTERN.match(ticker):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.invalid-ticker",
                message=f"ticker must be 4-char alphanumeric: {ticker!r}",
                location="ticker",
            )
        )
    playbook = front_matter.get("playbook")
    if isinstance(playbook, str) and playbook not in known_playbooks:
        known_sorted = sorted(known_playbooks)
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.unknown-playbook",
                message=(
                    f"playbook {playbook!r} has no schema in playbooks/; known: {known_sorted}"
                ),
                location="playbook",
            )
        )
    macro_gate = front_matter.get("macro_gate")
    if isinstance(macro_gate, str) and macro_gate not in KNOWN_MACRO_GATES:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.invalid-macro-gate",
                message=(
                    f"macro_gate must be one of {list(KNOWN_MACRO_GATES)}, got {macro_gate!r}"
                ),
                location="macro_gate",
            )
        )
    decision = front_matter.get("decision")
    if isinstance(decision, str):
        if decision not in KNOWN_DECISIONS:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.invalid-decision",
                    message=(f"decision must be one of {list(KNOWN_DECISIONS)}, got {decision!r}"),
                    location="decision",
                )
            )
    elif "decision" in front_matter:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.invalid-decision",
                message="decision must be a string",
                location="decision",
            )
        )
    override = front_matter.get("macro_gate_override")
    has_override = isinstance(override, str) and bool(override.strip())
    if "macro_gate_override" in front_matter and not has_override:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.invalid-macro-gate-override",
                message="macro_gate_override must be a non-empty string when present",
                location="macro_gate_override",
            )
        )
    if macro_gate == "headwind" and decision == "accepted":
        if has_override:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    target=path,
                    code="research.headwind-with-override",
                    message="accepted research uses headwind macro_gate with an explicit override",
                    location="macro_gate_override",
                )
            )
        else:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.headwind-without-override",
                    message=(
                        "accepted research with headwind macro_gate requires macro_gate_override"
                    ),
                    location="macro_gate_override",
                )
            )
    position_size = front_matter.get("position_size_oku")
    if isinstance(position_size, bool) or not isinstance(position_size, (int, float)):
        if "position_size_oku" in front_matter:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.invalid-position-size",
                    message="position_size_oku must be a positive number",
                    location="position_size_oku",
                )
            )
    elif position_size <= 0:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.invalid-position-size",
                message="position_size_oku must be greater than 0",
                location="position_size_oku",
            )
        )
    market_cap = front_matter.get("market_cap_oku")
    if isinstance(market_cap, bool) or not isinstance(market_cap, (int, float)):
        if "market_cap_oku" in front_matter:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.invalid-market-cap",
                    message="market_cap_oku must be a positive number",
                    location="market_cap_oku",
                )
            )
    elif market_cap <= 0:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.invalid-market-cap",
                message="market_cap_oku must be greater than 0",
                location="market_cap_oku",
            )
        )
    sector = front_matter.get("sector_33")
    if isinstance(sector, str):
        if not sector.strip():
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.invalid-sector",
                    message="sector_33 must be a non-empty string",
                    location="sector_33",
                )
            )
    elif "sector_33" in front_matter:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.invalid-sector",
                message="sector_33 must be a non-empty string",
                location="sector_33",
            )
        )
    if (
        decision == "accepted"
        and playbook == MEAN_REVERSION_PLAYBOOK
        and isinstance(market_cap, (int, float))
        and not isinstance(market_cap, bool)
        and 200 <= market_cap < 500
    ):
        if has_override:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    target=path,
                    code="research.low-cap-mean-reversion",
                    message=(
                        "P-A accepted research below 500 oku uses explicit "
                        "macro_gate_override; review P-B alternative"
                    ),
                    location="market_cap_oku",
                )
            )
        else:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.low-cap-mean-reversion",
                    message=(
                        "P-A is discouraged below 500 oku; consider "
                        "valuation-catalyst-confirmation-v1 (P-B) or document an "
                        "explicit macro_gate_override"
                    ),
                    location="market_cap_oku",
                )
            )
    valuation = front_matter.get("valuation")
    top_level_adv = front_matter.get("adv_participation_pct")
    if isinstance(top_level_adv, (int, float)) and not isinstance(top_level_adv, bool):
        _append_adv_participation_finding(path, top_level_adv, findings, "adv_participation_pct")
    elif "adv_participation_pct" in front_matter:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.invalid-adv-participation",
                message="adv_participation_pct must be a number when present",
                location="adv_participation_pct",
            )
        )
    if isinstance(valuation, dict):
        adv_participation = valuation.get("adv_participation_pct")
        if isinstance(adv_participation, (int, float)) and not isinstance(adv_participation, bool):
            _append_adv_participation_finding(
                path,
                adv_participation,
                findings,
                "valuation.adv_participation_pct",
            )
        elif "adv_participation_pct" in valuation:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.invalid-adv-participation",
                    message="valuation.adv_participation_pct must be a number when present",
                    location="valuation.adv_participation_pct",
                )
            )
    screened_ref = front_matter.get("screened_ref")
    if isinstance(screened_ref, str) and not screened_ref.endswith(".yaml"):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.screened-ref-not-yaml",
                message="screened_ref must end with .yaml (PR 1-A: screened is YAML)",
                location="screened_ref",
            )
        )
    view_ref = front_matter.get("view_ref")
    if isinstance(view_ref, str) and not view_ref.endswith(".md"):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.view-ref-not-md",
                message="view_ref must end with .md",
                location="view_ref",
            )
        )
    return findings


def _append_adv_participation_finding(
    path: Path,
    value: int | float,
    findings: list[ValidationFinding],
    location: str,
) -> None:
    if value >= 5.0:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.adv-participation-cap",
                message="adv_participation_pct must be below 5.0 for accepted research",
                location=location,
            )
        )
