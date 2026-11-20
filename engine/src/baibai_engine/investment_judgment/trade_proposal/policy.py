"""構造契約から分離した、現在の提案write policy。"""

from __future__ import annotations

from baibai_engine.investment_judgment.trade_proposal.contracts import (
    ProposalError,
    TradeProposalDraft,
)

MAX_NEW_BUYS = 1


# 旧雛形に予約されていた命令だけを識別する。文章の質や査読者の独立性の判定ではない。
_RESERVED_TEXT = {
    "提案の要点を記述してください。",
    "採らなかった行動とその理由を記述してください。",
    "買付では未約定・部分約定・新情報の後に何を再確認するかを記す。",
    "不足を確認して行動と理由を記述し、独立検証を行う。",
    "独立Reviewerのidentityを記述してください。",
}
# 新規/保有と可変の資料IDを含む旧定型文の両端。単語や語尾だけでは弾かない。
_RESERVED_FRAMES = (
    (
        "docs/reference/trade-proposal.md#limit-selectionに従い、",
        "選んだ行動と、それを変える次の価格または企業事実を記す。",
    ),
    (
        "維持を起点に、現金回収が目的なら回収額が近い案と過不足を比べる。",
        "選んだ行動と、それを変える次の価格または企業事実を記す。",
    ),
    (
        "観測価格で割安と判断したかと、同じ資金で今買う・指値で待つ・別案や現金を選ぶかを分ける。",
        "何が変わったかを記す。変わらない場合も区別する。",
    ),
    ("当初の仮説と今の終値からの残存見返り、", "何が変わったかを記す。変わらない場合も区別する。"),
    ("現物台帳の最終事実・終値・実口座を区別する。", "再確認する資料・時点を記す。"),
    ("相対強弱が理由なら、自己の絶対騰落と", "全シグナルのコメントは要求しない。"),
    ("前回の指値・対象日を今回へ流用せず、", "変えたか（変化なしも含む）を記す。"),
    ("JPXの保存原本（", "指定だけから買付けの成立、廃止日または支払日を決めない。"),
    ("EDINETの保存一覧に起点", "結果報告書の存在は成立や新規取得者への入金の証明ではない。"),
    ("後日に日別一覧が更新され、この保存コピーから", "当時の受領版と発行体の開示を確認する。"),
    (
        "今回のEDINET保存一覧ではこの対象の関連書類を特定できない。",
        "現在の提出書類・発行体開示を確認する。",
    ),
)


def _has_reserved_instruction(proposal: TradeProposalDraft) -> bool:
    texts = [proposal.headline, proposal.comparison, proposal.forgone]
    if proposal.review is not None:
        texts.append(proposal.review.reviewer_identity)
    for item in proposal.items:
        texts.extend((item.rationale, *item.review_conditions))
        if item.action == "defer":
            if any(reason.code == "proposal_content_requires_review" for reason in item.blockers):
                return True
            texts.extend(item.resume_conditions)
    return any(
        text in _RESERVED_TEXT
        or any(text.startswith(start) and text.endswith(end) for start, end in _RESERVED_FRAMES)
        for text in texts
    )


def validate_proposal_policy(proposal: TradeProposalDraft) -> None:
    """初期版の新規買付上限を検証する。readの構造上限にはしない。"""

    if _has_reserved_instruction(proposal):
        raise ProposalError(
            "replace reserved authoring instructions with authored proposal content"
        )
    if sum(item.action == "buy" for item in proposal.items) > MAX_NEW_BUYS:
        raise ProposalError(f"current proposal policy permits at most {MAX_NEW_BUYS} new buy")
