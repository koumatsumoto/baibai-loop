"""Detect AP-06 source_refs / rationale mismatches in outlook YAML.

For every ``rationale`` in ``records/02-outlook/**.yaml`` (sector / region /
change entry), extract numeric+unit tokens (e.g. ``5.09%``, ``164.34億円``) and
verify that *at least one* listed brief in ``source_refs`` contains each token
verbatim. Tokens not found in any cited brief are reported as findings.

This is heuristic: paraphrased prose tokens are not detected, but the most
common AP-06 failure pattern (a numeric quoted in rationale that does not
exist in the cited brief) is caught reliably. PR #77 round 1 (春闘 5.09% 言及
が CPI/retail brief 不整合) is the canonical example.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

# Tokens that are reliable signatures of facts:
# - decimal/integer percentages: ``5.09%`` / ``+0.5pt``
# - large-unit JPY counts: ``164.34億円`` / ``1,420 億円``
# - bp counts: ``80bp``
# Designed to be precise; paraphrased prose is intentionally out of scope.
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\d+(?:\.\d+)?\s*%"),
    re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?\s*(?:兆|億|百万|千)\s*(?:円|ドル|USD)"),
    re.compile(r"[+\-]?\d+(?:\.\d+)?\s*(?:bp|pt)"),
)

_RATIONALE_KEYS_IN_MAP: tuple[str, ...] = ("rationale",)


@dataclass(frozen=True, slots=True)
class OutlookFinding:
    """A single AP-06 mismatch surfaced by ``scan_outlook_source_refs``."""

    severity: str
    target: Path
    code: str
    message: str
    location: str
    token: str


def scan_outlook_source_refs(
    outlook_root: Path, *, repo_root: Path | None = None
) -> list[OutlookFinding]:
    """Scan all outlook YAML files for rationale ↔ source_refs token mismatches."""
    if not outlook_root.is_dir():
        return []
    repo = repo_root or outlook_root.parents[1]
    findings: list[OutlookFinding] = []
    for path in sorted(outlook_root.rglob("outlook-*.yaml")):
        findings.extend(_scan_one_outlook(path, repo))
    return findings


def _scan_one_outlook(path: Path, repo_root: Path) -> list[OutlookFinding]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError, yaml.YAMLError:
        # 既存 validator (validate/outlook.py) で別 finding として報告される。
        # precheck はこの段階では skip。
        return []
    if not isinstance(loaded, Mapping):
        return []
    findings: list[OutlookFinding] = []

    sectors = loaded.get("sectors")
    if isinstance(sectors, Mapping):
        for sector_name, payload in sectors.items():
            if isinstance(payload, Mapping):
                findings.extend(
                    _check_rationale_block(path, payload, f"sectors.{sector_name}", repo_root)
                )

    regions = loaded.get("regions")
    if isinstance(regions, Mapping):
        for region_name, payload in regions.items():
            if isinstance(payload, Mapping):
                findings.extend(
                    _check_rationale_block(path, payload, f"regions.{region_name}", repo_root)
                )

    changes = loaded.get("changes")
    if isinstance(changes, list):
        for index, payload in enumerate(changes):
            if isinstance(payload, Mapping):
                target = payload.get("target")
                location_suffix = (
                    f"changes[{index}]({target})"
                    if isinstance(target, str)
                    else f"changes[{index}]"
                )
                findings.extend(_check_rationale_block(path, payload, location_suffix, repo_root))

    return findings


def _check_rationale_block(
    path: Path, payload: Mapping[str, object], location_prefix: str, repo_root: Path
) -> list[OutlookFinding]:
    rationale = payload.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        return []
    source_refs = payload.get("source_refs")
    source_paths = _coerce_source_paths(source_refs, repo_root)
    if not source_paths:
        # rationale に fact があるのに source_refs が空 / 欠損なら報告。
        # 数値 token が無ければ noise なので無視。
        tokens = _collect_tokens(rationale)
        if not tokens:
            return []
        return [
            OutlookFinding(
                severity="warning",
                target=path,
                code="precheck.source-refs-empty",
                message=(f"rationale has fact tokens ({sorted(tokens)}) but source_refs is empty"),
                location=f"{location_prefix}.source_refs",
                token=", ".join(sorted(tokens)),
            )
        ]
    # source brief は token ごとに変わらないので、ループ前に 1 度だけ normalize する。
    # outlook 1 件で sectors 33 + regions 4 + changes が走るが、すべて同じ
    # source brief を参照することが多いため、ここで事前正規化しておくと全体で
    # O(rationale x token) -> O(brief + rationale x token) に下がる。
    normalized_source_texts = [_normalize(text) for text in _read_source_texts(source_paths)]
    findings: list[OutlookFinding] = []
    for token in _collect_tokens(rationale):
        normalized = _normalize(token)
        if not _matches_any_normalized(normalized, normalized_source_texts):
            findings.append(
                OutlookFinding(
                    severity="warning",
                    target=path,
                    code="precheck.token-not-in-source-refs",
                    message=(
                        f"token {token!r} appears in rationale but not in any cited source_ref"
                    ),
                    location=f"{location_prefix}.rationale",
                    token=token,
                )
            )
    return findings


def _collect_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for pattern in _TOKEN_PATTERNS:
        tokens.update(pattern.findall(text))
    return tokens


def _coerce_source_paths(value: object, repo_root: Path) -> list[Path]:
    if not isinstance(value, list):
        return []
    out: list[Path] = []
    for entry in value:
        if isinstance(entry, str) and entry.strip():
            out.append(repo_root / entry)
    return out


def _read_source_texts(paths: Iterable[Path]) -> list[str]:
    texts: list[str] = []
    for path in paths:
        try:
            texts.append(path.read_text(encoding="utf-8"))
        except OSError:
            # 取れない brief は別 validator が fail させるため precheck は無視。
            continue
    return texts


def _matches_any_normalized(normalized_token: str, normalized_source_texts: Iterable[str]) -> bool:
    return any(normalized_token in text for text in normalized_source_texts)


def _normalize(text: str) -> str:
    """Strip whitespace inside tokens so ``5.09%`` matches ``5.09 %`` etc."""
    return re.sub(r"\s+", "", text)
