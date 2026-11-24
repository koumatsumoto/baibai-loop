"""経済的な問いから同一時点の銘柄へ辿る。調査の入口であって割安判定ではない。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING, Any

from baibai_engine.investment_judgment.workflow.book_prices import BookPriceComparison
from baibai_engine.investment_judgment.workflow.selection import ResearchRequest
from baibai_engine.investment_judgment.workflow.selection_policy import CURRENT_RESEARCH_CAPACITY

if TYPE_CHECKING:
    from baibai_engine.machine_analysis.screening.discovery_contract import (
        DiscoveryIndexRead,
        DiscoveryRow,
    )


@dataclass(frozen=True, slots=True)
class _Question:
    question: str
    countercase: str


DISCOVERY_QUESTIONS = {
    "earning_power": _Question(
        "この価格に対し、報告利益のうち普通株に持続して残る額は大きいか。",
        "報告利益が一時的、他の持分へ帰属、または運転資金・投資に必要なら安くない。",
    ),
    "operating_recovery": _Question(
        "回復した営業利益は一時的な反動を超え、普通株の現金を増やすか。",
        "前年の落ち込みの反動・季節性・一時益だけでは、回復が続く根拠にならない。",
    ),
    "reinvestment": _Question(
        "増えた売上は、追加投資や希薄化を差し引いても普通株一株の稼ぎを増やすか。",
        "買収で売上が増えても、買収代金や追加の運転資金まで回収できるとは限らない。",
    ),
    "reported_assets": _Question(
        "報告された帳簿資本のうち、必要資本などを除き、誰がいつ普通株に返せるか。",
        "帳簿額は換金額ではない。資金の拘束、負債、非支配持分を確認し、普通株主への還元手段がなければ低いPBRだけで割安としない。",
    ),
    "capital_change": _Question(
        "記録された資本異動や株式基準の変更で、今取得する一株の取り分は変わったか。",
        "記録は上場状態・TOB・分配の確定ではない。新取得者が対象外の可能性もある。",
    ),
    "input_gap": _Question(
        "入力・価格基準の未確定を一次資料で解けば、価値差を調べられるか。",
        "不足の解消自体に価値はない。経路がなければ調査を打ち切ってよい。",
    ),
    "outside_display": _Question(
        "発行時の索引で経済仮説が立っていない会社に、割安と考える別の根拠はあるか。",
        "入力不足の会社も含み得る。仮説が立っていないだけでは、割安とも投資対象外とも判断できない。",
    ),
    "no_coordinate": _Question(
        "採用可能な財務・価格座標がない会社に、別の権利や資産から解ける問いはあるか。",
        "座標なしは会社の価値なしではない。資料の欠落・赤字・基準不一致の理由を区別する。",
    ),
    "universe": _Question(
        "記録されていない経路も含め、当時の対象会社から独立に照合できるか。",
        "母集団に含まれるだけでは、株価・権利・価値・執行可能性は確認できない。",
    ),
}


@dataclass(frozen=True, slots=True)
class PriceCoordinateMinimum:
    path: str
    variant: str
    group: str
    value: float

    def __post_init__(self) -> None:
        if not all((self.path, self.variant, self.group)) or not isfinite(self.value):
            raise ValueError(
                "a recorded price coordinate and a finite comparison value are required"
            )

    def compare(self, row: DiscoveryRow) -> str:
        matches = [
            c.get("price_value")
            for c in row["coordinates"]
            if (c.get("path_id"), c.get("variant"), c.get("price_group"))
            == (self.path, self.variant, self.group)
        ]
        if len(matches) != 1:
            return "not_comparable"
        value = matches[0]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            return "not_comparable"
        return "at_or_above" if value >= self.value else "below"

    def args(self) -> list[str]:
        return ["--price-coordinate-min", self.path, self.variant, self.group, str(self.value)]


def discovery_context(
    data: DiscoveryIndexRead,
    *,
    request: ResearchRequest,
    as_of: date,
    known_at: datetime,
    path: Path,
    db_path: Path | None,
    question: str | None,
    page: int,
    comparisons: tuple[str, ...] | None,
    minimum: PriceCoordinateMinimum | None,
    book_prices: BookPriceComparison | None = None,
) -> dict[str, Any]:
    if question is not None and question not in DISCOVERY_QUESTIONS:
        raise ValueError("unknown discovery question")
    result: dict[str, Any] = {
        "schema_version": 4,
        "request": request.model_dump(mode="json"),
        "research_as_of": as_of.isoformat(),
        "input_known_at": known_at.isoformat(),
        "generation_id": data.generation_id,
        "screening_selection_id": data.selection_id,
    }
    if data.status == "no_publication":
        return {
            **result,
            "status": "inputs_required",
            "missing_inputs": [{"code": "no_model_publication"}],
        }
    assert data.selection_id is not None
    result["generation_as_of"] = data.as_of.isoformat() if data.as_of else None
    result["evaluated_at"] = data.evaluation_time.isoformat() if data.evaluation_time else None
    result["published_at"] = data.published_at.isoformat() if data.published_at else None
    if data.status == "publication_without_index":
        return {
            **result,
            "status": "inputs_required",
            "missing_inputs": [{"code": "unindexed_legacy_publication"}],
            "meaning": "発行時の索引が未記録。候補0社ではなく、保存した証拠本文も開いていない。",
        }
    if data.status == "noncurrent_index":
        return {
            **result,
            "status": "inputs_required",
            "missing_inputs": [{"code": "current_discovery_index_required"}],
            "meaning": "最新公表物の索引が旧版。候補ゼロではなく、開始には現行索引の発行が必要。",
        }
    base = [
        "investment-judgment",
        "selection-context",
        "--scope",
        request.scope,
        "--request",
        request.text,
        "--screening-selection-id",
        data.selection_id,
        "--research-asof",
        as_of.isoformat(),
        "--known-at",
        known_at.isoformat(),
        "--runs-path",
        str(path),
    ]
    for ticker in request.requested_tickers:
        base.extend(["--ticker", ticker])
    if db_path is not None:
        base.extend(["--db", str(db_path)])
    result["repeat_command_args"] = base
    if comparisons is not None:
        # 索引にない明示銘柄も、元の世代のモデルの有無とは分けて照合する。
        requested = set(comparisons)
        rows = {row["ticker"]: row for row in data.rows if row["ticker"] in requested}
        return {
            **result,
            "status": "ready",
            "discovery": {
                "view": "explicit_evidence",
                "basis": "発行時の索引行と同じ世代の個社モデル。旧カードを再構成しない。",
                "rows": [rows[t] for t in comparisons if t in rows],
                "outside_universe": sorted(requested.difference(data.universe_tickers)),
            },
        }
    return {
        **result,
        "status": "ready",
        "capacity": CURRENT_RESEARCH_CAPACITY.model_dump(mode="json"),
        "discovery": _explore(
            data.rows,
            data.universe_tickers,
            question=question,
            page=page,
            minimum=minimum,
            book_prices=book_prices,
        ),
    }


def _members(row: DiscoveryRow) -> set[str]:
    routes = row["matched_paths"]
    financial, growth = set(routes["financial"]), set(routes["growth"])
    result = {"universe"}
    if financial & {"earning_power", "capital_productivity"} or "profit_growth" in growth:
        result.add("earning_power")
    if "operating_recovery" in financial or "operating_recovery" in growth:
        result.add("operating_recovery")
    if "business_expansion" in financial:
        result.add("reinvestment")
    book = row["book_equity"]
    amount = book.get("amount_yen") if book is not None else None
    if isinstance(amount, (int, float)) and not isinstance(amount, bool) and isfinite(amount):
        result.add("reported_assets")
    activity = row["capital_action"]
    if activity is not None and any(
        n is not None and n > 0
        for n in (activity["observations"], activity["share_basis_adjustments"])
    ):
        result.add("capital_change")
    price_observed = row["price_ready"] and row["price"] is not None
    if row["missing"] or row["financial_state"] == "input_unresolved" or not price_observed:
        result.add("input_gap")
    if not row["coordinates"]:
        result.add("no_coordinate")
    if row["screening_lane"] == "no_current_hypothesis":
        result.add("outside_display")
    return result


def _explore(
    rows: Sequence[DiscoveryRow],
    universe: Sequence[str],
    *,
    question: str | None,
    page: int,
    minimum: PriceCoordinateMinimum | None = None,
    book_prices: BookPriceComparison | None = None,
) -> dict[str, Any]:
    # 会社が複数の問いに関わる。重なる根拠を票・優先度や予算へ変換しない。
    counts: Counter[str] = Counter()
    eligible: list[DiscoveryRow] = []
    for row in rows:
        keys = _members(row)
        counts.update(keys)
        if question in keys:
            eligible.append(row)
    coverage = {
        "universe_count": len(universe),
        "indexed_rows": len(rows),
        "without_stock_models": sum(not row["models_recorded"] for row in rows),
        "outside_saved_lanes": counts["outside_display"],
        "capital_event_scope": "保存された資本異動等のみ。全市場のTOB・権利者・期日は非被覆。",
        "account_scope": "保有と単元で除外しない。保有・資金・注文可否は未確認。",
    }
    if question is None:
        return {
            "view": "choose_economic_question",
            "coverage": coverage,
            "meaning": "複数の問いに同じ会社を含む。問いから比較へ。件数は割安認定でない。",
            "questions": [
                {
                    "key": key,
                    "question": content.question,
                    "countercase": content.countercase,
                    "company_count": counts[key],
                    "append_args": ["--discovery-question", key],
                    **(
                        {"optional_comparison": "--reported-pbr-max <倍率>"}
                        if key == "reported_assets"
                        else {}
                    ),
                }
                for key, content in DISCOVERY_QUESTIONS.items()
            ],
        }
    content = DISCOVERY_QUESTIONS[question]
    args = ["--discovery-question", question]
    comparison: dict[str, Any] = {}
    if minimum is not None:
        states: Counter[str] = Counter()
        matched = []
        for row in eligible:
            state = minimum.compare(row)
            states[state] += 1
            if state == "at_or_above":
                matched.append(row)
        args.extend(minimum.args())
        comparison["coordinate_comparison"] = {
            "within_question": len(eligible),
            "at_or_above": states["at_or_above"],
            "below": states["below"],
            "not_comparable": states["not_comparable"],
            "specified_args": minimum.args(),
            "meaning": "同じ価格座標だけを比べる資料照会。要求利回り・投資適格性ではない。",
        }
        eligible = matched
    if book_prices is not None:
        observed = [r for r in eligible if r["ticker"] in book_prices.prices]
        selected = [
            r for r in observed if book_prices.prices[r["ticker"]].value <= book_prices.maximum
        ]
        args.extend(["--reported-pbr-max", str(book_prices.maximum)])
        comparison["reported_book_comparison"] = {
            "maximum": book_prices.maximum,
            "within_question": len(eligible),
            "outside_question": len(universe) - len(eligible),
            "at_or_below": len(selected),
            "above": len(observed) - len(selected),
            "unavailable": len(eligible) - len(observed),
            "meaning": (
                "保存した会社財務のPBR。比較先は上限で絞る前の全普通株または同一33業種から自己を除く。"
                "中央値は各社等重みで指数の加重PBRではない。同一株価日のうち、same_book_basisは"
                "簿価の期末・算出法・連単・会計基準まで一致する他社だけ。赤字を除かず、"
                "銘柄の事業や資産の回収率・原価・株主への配分、売買時期が等しいことは意味しない。"
            ),
        }
        eligible = selected
    result_page = _page(eligible, args, page)
    if book_prices is not None:
        for item in result_page["rows"]:
            item["book_price_comparison"] = book_prices.detail(item["ticker"])
    return {
        "view": "companies_for_question",
        "coverage": coverage,
        "key": question,
        "question": content.question,
        "countercase": content.countercase,
        "order": "銘柄コード順の閲覧用。先頭・所属経路の数・相対騰落を魅力度にしない。",
        **comparison,
        **result_page,
    }


def _page(rows: list[DiscoveryRow], args: list[str], number: int) -> dict[str, Any]:
    page_size = 24  # 転送単位。調査予算ではない。
    start = number * page_size
    ordered = sorted(rows, key=lambda row: row["ticker"])
    return {
        "total": len(ordered),
        "page": number,
        "next_args": [*args, "--screening-page", str(number + 1)]
        if start + page_size < len(ordered)
        else None,
        "rows": [
            {
                "ticker": row["ticker"],
                "observations": row,
                "detail_args": ["--compare-ticker", row["ticker"]],
            }
            for row in ordered[start : start + page_size]
        ],
    }
