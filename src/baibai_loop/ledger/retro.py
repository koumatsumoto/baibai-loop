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
    "混雑",
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
    paper_records = read_jsonl(root / "ledger" / "paper" / f"{month}.jsonl")
    skipped_records = read_jsonl(root / "ledger" / "skipped" / f"{month}.jsonl")
    reviews, warnings = _load_reviews(root, month)

    closed_trades = len(reviews)
    total_trades = max(len(paper_records), closed_trades)
    open_trades = max(total_trades - closed_trades, 0)
    skipped_candidates = len(skipped_records) + sum(
        1 for record in paper_records if record.get("decision") == "pending"
    )

    pnl_values = [_to_float(review.get("pnl_pct")) for review in reviews]
    realized_pnl = [value for value in pnl_values if value is not None]
    wins = sum(1 for value in realized_pnl if value > 0)
    losses = sum(1 for value in realized_pnl if value < 0)
    failure_counts = _count_classes(reviews, "failure_class", _FAILURE_CLASSES)
    success_counts = _count_classes(reviews, "success_class", _SUCCESS_CLASSES)
    price_missing_counts = _price_missing_counts((*paper_records, *skipped_records))

    front = {
        "retro_month": month,
        "total_trades": total_trades,
        "open_trades": open_trades,
        "closed_trades": closed_trades,
        "skipped_candidates": skipped_candidates,
        "wins": wins,
        "losses": losses,
        "pnl_pct_sum": round(sum(realized_pnl), 4),
        "failure_class_counts": failure_counts,
        "success_class_counts": success_counts,
        "playbook_revision_decision": "v1 据え置き",
        "next_cycle_changes": ["サンプル不足のため、次周回も ledger / review の記録品質を優先する"],
        "price_missing_counts": price_missing_counts,
    }
    content = _render_retro(month, front, paper_records, skipped_records, reviews)
    year = month[:4]
    return RetroDraft(
        path=root / "reviews" / year / f"retro-{month.replace('-', '')}.md",
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
    reviews_dir = root / "reviews" / month[:4] / month[5:7]
    if not reviews_dir.exists():
        warnings.append(f"{reviews_dir} does not exist; retro uses ledger-only fallback")
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
    paper_records: Sequence[Mapping[str, Any]],
    skipped_records: Sequence[Mapping[str, Any]],
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
        f"- 総 trade 数: {front['total_trades']} 件",
        f"- Open trade 数: {front['open_trades']} 件",
        f"- Closed trade 数: {front['closed_trades']} 件",
        f"- Skipped candidates: {front['skipped_candidates']} 件",
        f"- 勝敗: Wins {front['wins']} / Losses {front['losses']}",
        f"- P&L sum: {_format_pct(front['pnl_pct_sum'])}",
        "",
        "| Ticker | Playbook | Decision | Baseline | +15bd | +30bd |",
        "| --- | --- | --- | ---: | ---: | ---: |",
        *_paper_rows(paper_records),
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
        "## Skipped trade log の分析",
        "",
        _missing_price_summary(front["price_missing_counts"]),
        "",
        "| Ticker | Playbook | Baseline | +15bd | +30bd | +30bd 判定 |",
        "| --- | --- | ---: | ---: | ---: | --- |",
        *_skipped_rows(skipped_records),
        "",
        "## Macro gate 判定精度",
        "",
        *_macro_gate_lines((*paper_records, *skipped_records)),
        "",
        "## Playbook 改訂判断",
        "",
        f"- 改訂判断: {front['playbook_revision_decision']}",
        "- Playbook 別サンプル数:",
        *_playbook_count_lines((*paper_records, *skipped_records)),
        "- 10 件未満の playbook は v1 据え置きを許容する。",
        "",
        "## 次周回の運用変更点",
        "",
        *[f"- {item}" for item in front["next_cycle_changes"]],
    ]
    if reviews:
        lines.extend(("", "### 参照 review", "", *_review_lines(reviews)))
    return "\n".join(lines) + "\n"


def _paper_rows(records: Sequence[Mapping[str, Any]]) -> list[str]:
    if not records:
        return ["| - | - | - | - | - | - |"]
    return [
        "| {ticker} | {playbook} | {decision} | {baseline} | {plus15} | {plus30} |".format(
            ticker=record.get("ticker", ""),
            playbook=record.get("playbook", ""),
            decision=record.get("decision", ""),
            baseline=_format_price(record.get("baseline_price")),
            plus15=_format_price(_tracking_value(record, "plus_15bd")),
            plus30=_format_price(_tracking_value(record, "plus_30bd")),
        )
        for record in records
    ]


def _skipped_rows(records: Sequence[Mapping[str, Any]]) -> list[str]:
    if not records:
        return ["| - | - | - | - | - | - |"]
    return [
        "| {ticker} | {playbook} | {baseline} | {plus15} | {plus30} | {verdict} |".format(
            ticker=record.get("ticker", ""),
            playbook=record.get("playbook", ""),
            baseline=_format_price(record.get("baseline_price")),
            plus15=_format_return(record, "plus_15bd"),
            plus30=_format_return(record, "plus_30bd"),
            verdict=_skipped_verdict(record),
        )
        for record in records
    ]


def _class_rows(counts: object) -> list[str]:
    if not isinstance(counts, Mapping):
        return []
    return [f"| {name} | {count} |" for name, count in counts.items()]


def _macro_gate_lines(records: Sequence[Mapping[str, Any]]) -> list[str]:
    grouped: dict[str, list[float]] = {}
    for record in records:
        macro_gate = str(record.get("macro_gate") or "unknown")
        value = _return_pct(record, "plus_15bd")
        if value is not None:
            grouped.setdefault(macro_gate, []).append(value)
    if not grouped:
        return ["- +15bd が未解決のため、macro gate 別の定量評価は未算出。"]
    return [
        f"- {gate}: +15bd 平均 {_format_pct(sum(values) / len(values))} ({len(values)} 件)"
        for gate, values in sorted(grouped.items())
    ]


def _playbook_count_lines(records: Sequence[Mapping[str, Any]]) -> list[str]:
    counts = Counter(str(record.get("playbook") or "unknown") for record in records)
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


def _skipped_verdict(record: Mapping[str, Any]) -> str:
    return_pct = _return_pct(record, "plus_30bd")
    if return_pct is None:
        return "未判定"
    return "偽陰性候補" if return_pct > 0 else "妥当候補"


def _format_return(record: Mapping[str, Any], key: str) -> str:
    value = _return_pct(record, key)
    return _format_pct(value) if value is not None else "-"


def _return_pct(record: Mapping[str, Any], key: str) -> float | None:
    baseline = _to_float(record.get("baseline_price"))
    target = _to_float(_tracking_value(record, key))
    if baseline is None or baseline == 0 or target is None:
        return None
    return round((target / baseline - 1) * 100, 4)


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


def _format_pct(value: object) -> str:
    numeric = _to_float(value)
    if numeric is None:
        return "-"
    sign = "+" if numeric > 0 else ""
    return f"{sign}{numeric:.2f}%"


def _to_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None
