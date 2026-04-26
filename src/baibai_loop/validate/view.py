"""Validate view markdown front matter.

view markdown は人間 narrative が中心の成果物。Markdown body は形式自由
にし、front matter の `sectors` / `regions` のみを機械的に検証する。
未知 sector / 未知 region は warning、不正な status (許容値以外) は error。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

import yaml

from .errors import ValidationFinding

KNOWN_VIEW_STATES: tuple[str, ...] = ("tailwind", "neutral", "headwind")
KNOWN_REGIONS: tuple[str, ...] = (
    "us",
    "japan-domestic",
    "japan-external-demand",
    "emerging",
)
# 33 業種の正規名 (東証 33 業種)。view が部分集合を書くこと自体は許容するが、
# 表記ゆれ (例: 末尾の中黒抜け) を warning で検出する。
KNOWN_SECTORS: tuple[str, ...] = (
    "水産・農林業",
    "鉱業",
    "建設業",
    "食料品",
    "繊維製品",
    "パルプ・紙",
    "化学",
    "医薬品",
    "石油・石炭製品",
    "ゴム製品",
    "ガラス・土石製品",
    "鉄鋼",
    "非鉄金属",
    "金属製品",
    "機械",
    "電気機器",
    "輸送用機器",
    "精密機器",
    "その他製品",
    "電気・ガス業",
    "陸運業",
    "海運業",
    "空運業",
    "倉庫・運輸関連業",
    "情報・通信業",
    "卸売業",
    "小売業",
    "銀行業",
    "証券、商品先物取引業",
    "保険業",
    "その他金融業",
    "不動産業",
    "サービス業",
)

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


def validate_view_file(path: Path) -> list[ValidationFinding]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="view.io",
                message=f"failed to read file: {exc}",
            )
        ]
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="view.no-front-matter",
                message="view markdown must start with `---` YAML front matter",
            )
        ]
    try:
        front_matter = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="view.invalid-yaml",
                message=f"front matter YAML parse failed: {exc}",
            )
        ]
    if not isinstance(front_matter, dict):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="view.front-matter-non-mapping",
                message="view front matter must be a mapping",
            )
        ]
    findings: list[ValidationFinding] = []
    findings.extend(_check_sectors(path, front_matter.get("sectors")))
    findings.extend(_check_regions(path, front_matter.get("regions")))
    return findings


def discover_view_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("view-*.md") if p.is_file())


def _check_sectors(path: Path, value: object) -> list[ValidationFinding]:
    if value is None:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="view.missing-sectors",
                message="front matter must define `sectors`",
                location="sectors",
            )
        ]
    if not isinstance(value, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="view.sectors-not-mapping",
                message="`sectors` must be a mapping of sector → status",
                location="sectors",
            )
        ]
    findings: list[ValidationFinding] = []
    known_sectors = set(KNOWN_SECTORS)
    for sector, status in value.items():
        sector_str = str(sector)
        if sector_str not in known_sectors:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    target=path,
                    code="view.unknown-sector",
                    message=f"unknown sector name: {sector_str!r}",
                    location=f"sectors.{sector_str}",
                )
            )
        if not isinstance(status, str) or status not in KNOWN_VIEW_STATES:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="view.invalid-sector-status",
                    message=(
                        f"sector status must be one of {list(KNOWN_VIEW_STATES)}, got {status!r}"
                    ),
                    location=f"sectors.{sector_str}",
                )
            )
    return findings


def _check_regions(path: Path, value: object) -> list[ValidationFinding]:
    if value is None:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="view.missing-regions",
                message="front matter must define `regions`",
                location="regions",
            )
        ]
    if not isinstance(value, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="view.regions-not-mapping",
                message="`regions` must be a mapping",
                location="regions",
            )
        ]
    findings: list[ValidationFinding] = []
    known_regions = set(KNOWN_REGIONS)
    for region, status in value.items():
        region_str = str(region)
        if region_str not in known_regions:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    target=path,
                    code="view.unknown-region",
                    message=f"unknown region name: {region_str!r}",
                    location=f"regions.{region_str}",
                )
            )
        if status is None:
            continue
        if not isinstance(status, str) or status not in KNOWN_VIEW_STATES:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="view.invalid-region-status",
                    message=(
                        f"region status must be one of {list(KNOWN_VIEW_STATES)} or null, "
                        f"got {status!r}"
                    ),
                    location=f"regions.{region_str}",
                )
            )
    return findings
