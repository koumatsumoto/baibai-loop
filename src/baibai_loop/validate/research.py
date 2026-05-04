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
    "candidates_ref",
    "outlook_ref",
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
    return validate_research_parsed(
        path,
        front_matter,
        body,
        playbooks_root=playbooks_root,
        known_playbooks=known_playbooks,
    )


def validate_research_parsed(
    path: Path,
    front_matter: dict[str, object],
    body: str,
    *,
    playbooks_root: Path | None = None,
    known_playbooks: frozenset[str] | None = None,
) -> list[ValidationFinding]:
    """Validate already-parsed research front matter and body.

    CLI 側は load_research_document の結果をキャッシュしてから collection 集約と
    per-file 検証の両方で再利用する。単独呼び出し用に validate_research_file が
    薄いラッパーとして残るが、内側のロジックは本関数に集約する。
    """
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


def load_research_document(
    path: Path,
) -> tuple[dict[str, object], str] | list[ValidationFinding]:
    """Public entry to ``_load_research_document`` for CLI-level caching."""
    return _load_research_document(path)


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
    return Path(__file__).resolve().parents[3] / "records" / "_playbooks"


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
                    f"playbook {playbook!r} has no schema in records/_playbooks/; "
                    f"known: {known_sorted}"
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
                    message="position_size_oku must be a non-negative number",
                    location="position_size_oku",
                )
            )
    elif position_size < 0:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.invalid-position-size",
                message="position_size_oku must be non-negative",
                location="position_size_oku",
            )
        )
    elif position_size == 0 and decision != "skipped":
        # accepted / pending は実 position を伴うため 0 は不可。skipped のみ 0 を許容し、
        # ヒューリスティック値は hypothetical_position_size_oku に分離する設計を許す。
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.invalid-position-size",
                message=(
                    "position_size_oku must be > 0 for accepted/pending decision; "
                    "use 0 only with decision=skipped"
                ),
                location="position_size_oku",
            )
        )
    elif position_size > 0 and decision == "skipped":
        # skipped 判定で実 position 値を残すと ledger sync (`src/baibai_loop/ledger/
        # sync.py`) が skipped ledger の adv_participation_pct を計算してしまう。
        # 実建玉なしを示す skipped では position_size_oku: 0 を強制し、参考値は
        # hypothetical_position_size_oku に分離する。
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.invalid-position-size",
                message=(
                    "decision=skipped requires position_size_oku=0; use "
                    "hypothetical_position_size_oku for reference values"
                ),
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
        _append_adv_participation_avg_turnover_required_finding(
            path, front_matter, findings, "adv_participation_pct"
        )
        _append_adv_participation_consistency_finding(
            path, front_matter, top_level_adv, findings, "adv_participation_pct"
        )
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
            _append_adv_participation_avg_turnover_required_finding(
                path, front_matter, findings, "valuation.adv_participation_pct"
            )
            _append_adv_participation_consistency_finding(
                path, front_matter, adv_participation, findings, "valuation.adv_participation_pct"
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
    candidates_ref = front_matter.get("candidates_ref")
    if isinstance(candidates_ref, str) and not candidates_ref.endswith(".yaml"):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.candidates-ref-not-yaml",
                message="candidates_ref must end with .yaml",
                location="candidates_ref",
            )
        )
    if isinstance(candidates_ref, str) and candidates_ref.endswith(".yaml"):
        ticker = front_matter.get("ticker")
        front_avg_turnover = front_matter.get("avg_turnover_oku")
        if (
            isinstance(ticker, str)
            and isinstance(front_avg_turnover, (int, float))
            and not isinstance(front_avg_turnover, bool)
            and front_avg_turnover > 0
        ):
            _append_avg_turnover_candidates_consistency_finding(
                path, candidates_ref, ticker, front_avg_turnover, findings
            )
    outlook_ref = front_matter.get("outlook_ref")
    if isinstance(outlook_ref, str) and not outlook_ref.endswith(".yaml"):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.outlook-ref-not-yaml",
                message="outlook_ref must end with .yaml",
                location="outlook_ref",
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


def _append_avg_turnover_candidates_consistency_finding(
    path: Path,
    candidates_ref: str,
    ticker: str,
    front_avg_turnover: int | float,
    findings: list[ValidationFinding],
) -> None:
    """research front matter の avg_turnover_oku が candidates_ref の対応 ticker と
    整合しているか check する。

    ledger sync (`src/baibai_loop/ledger/sync.py`) は candidate YAML の avg_turnover_oku
    を使って adv_participation_pct を再計算するため、front matter と candidate がずれて
    いると validator が通っても ledger は別の値で計算する穴になる。

    candidate YAML の解決は repo root を起点とした相対 path で行う。candidate file が
    存在しない / ticker が見つからない / candidate に avg_turnover_oku が無い場合は
    silently skip (warning にしない: 既存テスト fixture や archive 対応のため)。
    """
    repo_root = _resolve_repo_root(path)
    candidate_path = repo_root / candidates_ref
    if not candidate_path.is_file():
        return
    try:
        candidate_doc = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
    except OSError, yaml.YAMLError:
        return
    if not isinstance(candidate_doc, dict):
        return
    tickers = candidate_doc.get("tickers")
    if not isinstance(tickers, list):
        return
    for entry in tickers:
        if not isinstance(entry, dict):
            continue
        if entry.get("ticker") != ticker:
            continue
        candidate_avg = entry.get("avg_turnover_oku")
        if not isinstance(candidate_avg, (int, float)) or isinstance(candidate_avg, bool):
            return
        if candidate_avg <= 0:
            return
        diff_ratio = abs(front_avg_turnover - candidate_avg) / candidate_avg
        if diff_ratio > 0.05:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    target=path,
                    code="research.avg-turnover-candidates-mismatch",
                    message=(
                        f"front matter avg_turnover_oku={front_avg_turnover} mismatches "
                        f"candidates_ref={candidates_ref} ticker={ticker} value "
                        f"{candidate_avg} (diff {diff_ratio * 100:.1f}% > 5%); "
                        f"ledger sync uses candidate value, validator uses front value"
                    ),
                    location="avg_turnover_oku",
                )
            )
        return


def _resolve_repo_root(path: Path) -> Path:
    """research file path から repo root を推定する。`records/04-research/...` 構造を想定。"""
    resolved = path.resolve()
    for parent in resolved.parents:
        if (parent / "records").is_dir() and (parent / "docs").is_dir():
            return parent
    return resolved.parent


def _append_adv_participation_avg_turnover_required_finding(
    path: Path,
    front_matter: dict[str, object],
    findings: list[ValidationFinding],
    location: str,
) -> None:
    """adv_participation_pct があるなら avg_turnover_oku の正値併記を必須にする。

    avg_turnover_oku が無い / 0 / 負値だと整合チェック (position_size / avg_turnover *
    100) が skip され、100 倍ズレ等の桁誤りを catch できない。「数値であれば OK」では
    なく「正値 (> 0)」を必須にする。
    """
    avg_turnover = front_matter.get("avg_turnover_oku")
    if (
        isinstance(avg_turnover, (int, float))
        and not isinstance(avg_turnover, bool)
        and avg_turnover > 0
    ):
        return
    findings.append(
        ValidationFinding(
            severity="error",
            target=path,
            code="research.missing-avg-turnover-oku",
            message=(
                "adv_participation_pct requires avg_turnover_oku > 0 in front matter for "
                "consistency check (prevents 100x scaling errors and divide-by-zero skips)"
            ),
            location=location,
        )
    )


def _append_adv_participation_consistency_finding(
    path: Path,
    front_matter: dict[str, object],
    adv_participation: int | float,
    findings: list[ValidationFinding],
    location: str,
) -> None:
    """Cross-check adv_participation_pct against position_size_oku / avg_turnover_oku.

    `position_size_oku / avg_turnover_oku * 100 ≈ adv_participation_pct` を確認する
    (許容誤差 5%)。100 倍ズレなどの桁誤りを検出する。

    依存先 field の状態別の挙動:
    - `position_size_oku` 未指定 / 非数値: required check 側で別途 error 化されるので skip
    - `avg_turnover_oku <= 0` または不在: required check 側で error 化されるので skip
    - `position_size_oku == 0` (skipped packet 想定): expected = 0 となるので、
      adv_participation_pct も `0` でなければ error にする (skipped で hypothetical 値が
      混入する穴を塞ぐ)
    """
    position_size = front_matter.get("position_size_oku")
    avg_turnover = front_matter.get("avg_turnover_oku")
    if not isinstance(position_size, (int, float)) or isinstance(position_size, bool):
        return
    if not isinstance(avg_turnover, (int, float)) or isinstance(avg_turnover, bool):
        return
    if avg_turnover <= 0:
        # required check 側で error 化済み。consistency 計算は分母不正のため skip
        return
    expected = (position_size / avg_turnover) * 100.0
    if expected == 0:
        # position_size_oku == 0 (skipped) の場合、adv_participation_pct も 0 を要求
        if adv_participation != 0:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.adv-participation-inconsistent",
                    message=(
                        f"position_size_oku=0 requires adv_participation_pct=0 but got "
                        f"{adv_participation}; use hypothetical_position_size_oku for "
                        f"reference values in skipped packets"
                    ),
                    location=location,
                )
            )
        return
    diff_ratio = abs(adv_participation - expected) / expected
    if diff_ratio > 0.05:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.adv-participation-inconsistent",
                message=(
                    f"adv_participation_pct={adv_participation:.6f} does not match "
                    f"position_size_oku / avg_turnover_oku * 100={expected:.6f} "
                    f"(diff {diff_ratio * 100:.1f}% > 5%); likely scaling error"
                ),
                location=location,
            )
        )
