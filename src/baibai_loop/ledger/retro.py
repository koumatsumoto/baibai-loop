from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from baibai_loop.screening.filesystem import write_text_atomic

from .io import read_jsonl

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)
_FAILURE_CLASSES = (
    "材料誤読",
    "既に織り込み済み",
    "マクロ逆風",
    "ポジショニング / 流動性",
    "流動性不足",
    "ルール違反",
)
_SUCCESS_CLASSES = ("仮説的中", "catalyst 反応", "macro tailwind", "timing 一致")


@dataclass(frozen=True, slots=True)
class RetroDraft:
    path: Path
    content: str
    warnings: tuple[str, ...]


def build_monthly_retro(root: Path, month: str) -> RetroDraft:
    _validate_month(month)
    decisions = read_jsonl(root / "records/_ledger" / "research-decisions" / f"{month}.jsonl")
    reviews, warnings = _load_reviews(root, month)

    approved_decisions = sum(1 for record in decisions if _research_outcome(record) == "approved")
    submitted_orders = sum(1 for record in decisions if _has_order_submission(record))
    filled_positions = sum(
        1
        for record in decisions
        if record.get("trade_execution_state") in {"filled", "partially_filled"}
    )
    closed_positions = sum(
        1 for review in reviews if review.get("classification") in {"success", "failure"}
    )
    missed_opportunities = sum(
        1 for record in decisions if _research_outcome(record) in {"rejected", "deferred"}
    )

    failure_counts = _count_classes(reviews, "failure_class", _FAILURE_CLASSES)
    success_counts = _count_classes(reviews, "success_class", _SUCCESS_CLASSES)
    price_missing_counts = _price_missing_counts(decisions)
    front = {
        "retro_month": month,
        "approved_decisions": approved_decisions,
        "submitted_orders": submitted_orders,
        "filled_positions": filled_positions,
        "closed_positions": closed_positions,
        "missed_opportunities": missed_opportunities,
        "failure_class_counts": failure_counts,
        "success_class_counts": success_counts,
        "playbook_revision_decision": "据え置き",
        "next_cycle_changes": [
            "サンプル不足のため、次周回も decision register / review の記録品質を優先する"
        ],
        "price_missing_counts": price_missing_counts,
    }
    content = _render_retro(month, front, decisions, reviews)
    year = month[:4]
    return RetroDraft(
        path=root / "records/07-reviews" / year / f"retro-{month.replace('-', '')}.md",
        content=content,
        warnings=tuple(warnings),
    )


def write_monthly_retro(root: Path, month: str, *, overwrite: bool = False) -> RetroDraft:
    draft = build_monthly_retro(root, month)
    if draft.path.exists() and not overwrite:
        raise FileExistsError(f"retro draft already exists: {draft.path}")
    write_text_atomic(draft.path, draft.content)
    return draft


def _validate_month(month: str) -> None:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
        raise ValueError(f"invalid month: {month}")
    date.fromisoformat(f"{month}-01")


def _load_reviews(root: Path, month: str) -> tuple[list[dict[str, Any]], list[str]]:
    reviews: list[dict[str, Any]] = []
    warnings: list[str] = []
    reviews_dir = root / "records/07-reviews" / month[:4] / month[5:7]
    if not reviews_dir.exists():
        warnings.append(f"{reviews_dir} does not exist; retro uses decision-register fallback")
        return reviews, warnings

    for path in sorted(reviews_dir.glob("*.md")):
        if path.name == "template.md" or path.name.startswith("retro-"):
            continue
        match = _FRONT_MATTER_RE.match(path.read_text(encoding="utf-8"))
        if not match:
            warnings.append(f"skip malformed review file without front matter: {path}")
            continue
        payload = yaml.safe_load(match.group(1))
        if not isinstance(payload, dict):
            warnings.append(f"skip review file with non-mapping front matter: {path}")
            continue
        review = dict(payload)
        review["_path"] = str(path.relative_to(root))
        reviews.append(review)
    return reviews, warnings


def _count_classes(
    reviews: Sequence[Mapping[str, Any]],
    key: str,
    known_classes: Sequence[str],
) -> dict[str, int]:
    counts = Counter(str(review[key]) for review in reviews if review.get(key))
    return {class_name: counts[class_name] for class_name in known_classes}


def _price_missing_counts(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    missing = {"plus_15bd": 0, "plus_30bd": 0}
    for record in records:
        tracking = record.get("tracking")
        if not isinstance(tracking, Mapping):
            missing["plus_15bd"] += 1
            missing["plus_30bd"] += 1
            continue
        for key in missing:
            if tracking.get(key) is None:
                missing[key] += 1
    return missing


def _render_retro(
    month: str,
    front: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    reviews: Sequence[Mapping[str, Any]],
) -> str:
    front_yaml = yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
    lines = [
        "---",
        front_yaml.rstrip(),
        "---",
        "",
        f"# Retro: {month} 月次振り返り",
        "",
        "## Trade 集計",
        "",
        f"- Approved decisions: {front['approved_decisions']} 件",
        f"- Submitted orders: {front['submitted_orders']} 件",
        f"- Filled positions: {front['filled_positions']} 件",
        f"- Closed positions: {front['closed_positions']} 件",
        f"- Missed opportunities: {front['missed_opportunities']} 件",
        "",
        "| Ticker | Playbook | Scope | Research outcome | +15bd | +30bd |",
        "| --- | --- | --- | --- | ---: | ---: |",
        *_decision_rows(decisions),
        "",
        "## 失敗分類の集計",
        "",
        "| 分類 | 件数 |",
        "| --- | ---: |",
        *_class_rows(front["failure_class_counts"]),
        "",
        "## 成功分類の集計",
        "",
        "| 分類 | 件数 |",
        "| --- | ---: |",
        *_class_rows(front["success_class_counts"]),
        "",
        "## Missed opportunity tracking の分析",
        "",
        _missing_price_summary(front["price_missing_counts"]),
        "",
        "## Macro context fit 判定精度",
        "",
        *_macro_context_lines(decisions),
        "",
        "## Playbook 改訂判断",
        "",
        f"- 改訂判断: {front['playbook_revision_decision']}",
        "- Playbook 別サンプル数:",
        *_playbook_count_lines(decisions),
        "- 10 件未満の playbook は据え置きを許容する。",
        "",
        "## 次周回の運用変更点",
        "",
        *[f"- {item}" for item in front["next_cycle_changes"]],
    ]
    if reviews:
        lines.extend(("", "### 参照 review", "", *_review_lines(reviews)))
    return "\n".join(lines) + "\n"


def _decision_rows(records: Sequence[Mapping[str, Any]]) -> list[str]:
    if not records:
        return ["| - | - | - | - | - | - |"]
    row_template = "| {ticker} | {playbook} | {scope} | {outcome} | {plus15} | {plus30} |"
    return [
        row_template.format(
            ticker=record.get("ticker", ""),
            playbook=record.get("playbook_id", ""),
            scope=record.get("decision_scope", ""),
            outcome=_research_outcome(record) or "",
            plus15=_format_price(_tracking_value(record, "plus_15bd")),
            plus30=_format_price(_tracking_value(record, "plus_30bd")),
        )
        for record in records
    ]


def _class_rows(counts: object) -> list[str]:
    if not isinstance(counts, Mapping):
        return []
    return [f"| {name} | {count} |" for name, count in counts.items()]


def _macro_context_lines(records: Sequence[Mapping[str, Any]]) -> list[str]:
    counts = Counter(
        str(record.get("macro_context_decision_effect") or "unknown")
        for record in records
        if record.get("decision_scope") == "research_memo"
    )
    if not counts:
        return ["- macro context fit 別の定量評価は未算出。"]
    return [f"- {effect}: {count} 件" for effect, count in sorted(counts.items())]


def _playbook_count_lines(records: Sequence[Mapping[str, Any]]) -> list[str]:
    counts = Counter(str(record.get("playbook_id") or "unknown") for record in records)
    if not counts:
        return ["  - none: 0 件"]
    return [f"  - {playbook}: {count} 件" for playbook, count in sorted(counts.items())]


def _review_lines(reviews: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        f"- {review.get('_path')}: classification={review.get('classification', 'unknown')}"
        for review in reviews
    ]


def _missing_price_summary(value: object) -> str:
    if not isinstance(value, Mapping):
        return "- 価格欠損: +15bd 0 件 / +30bd 0 件"
    return (
        f"- 価格欠損: +15bd {value.get('plus_15bd', 0)} 件 / +30bd {value.get('plus_30bd', 0)} 件"
    )


def _research_outcome(record: Mapping[str, Any]) -> str | None:
    decision = record.get("research_decision")
    if isinstance(decision, Mapping):
        outcome = decision.get("outcome")
        return str(outcome) if outcome is not None else None
    return None


def _has_order_submission(record: Mapping[str, Any]) -> bool:
    if record.get("decision_scope") != "trade_execution":
        return False
    state = record.get("trade_execution_state")
    return state in {"submitted", "partially_filled", "filled"}


def _tracking_value(record: Mapping[str, Any], key: str) -> object:
    tracking = record.get("tracking")
    if isinstance(tracking, Mapping):
        return tracking.get(key)
    return None


def _format_price(value: object) -> str:
    numeric = _to_float(value)
    if numeric is None:
        return "-"
    return f"{numeric:.2f}"


def _to_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None
