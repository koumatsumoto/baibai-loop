"""固定scope/入力から執筆draftを作る。未記述の比較を経済的な見送りにしない。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from baibai_engine.investment_judgment.trade_proposal.contracts import (
    DeferItem,
    GateReason,
    ProposalContentReview,
    ProposalItem,
    TradeProposalDraft,
)
from baibai_engine.investment_judgment.trade_proposal.inputs import build_proposal_context
from baibai_engine.investment_judgment.trade_proposal.warnings import build_proposal_warnings
from baibai_engine.primitives.time import JST


def scaffold_proposal(
    *,
    operation_id: str,
    proposal_id: str,
    db_path: Path | None,
    sqlite_path: Path,
    now: datetime,
) -> dict[str, object]:
    context = build_proposal_context(
        operation_id=operation_id,
        proposal_id=proposal_id,
        db_path=db_path,
        sqlite_path=sqlite_path,
        now=now,
    )
    if context.research_scope.targets[0].purpose == "holding":
        decision = (
            "当初の仮説と現値からの残存見返りを別々に確認し、保有継続・売却・判断保留を比べる。"
        )
        terms_review = (
            "売却では回収下限・未約定時の継続保有を比べ、判断不足では再確認する事実と時点を記す。"
        )
        alternative = "売却下限での回収と保有継続・回復待ちを比べる。"
    else:
        decision = "今買う・指値で待つ・現金を選ぶ・調査を保留する理由を区別する。"
        terms_review = "買付では未約定・部分約定・新情報の後に何を再確認するかを記す。"
        alternative = (
            "docs/reference/trade-proposal.md#limit-selectionに従い、指値と数量、残cash、"
            "no-addで増量できない代償を、現金維持で逃す反発・回避できる損失と比べる。"
        )
    items: list[ProposalItem] = []
    previous_targets = context.previous_buy_context.get("targets")
    for outcome in context.research_scope.outcomes:
        if outcome.status == "unfinished":
            reference = None
            rationale = message = outcome.unfinished_reason
            code = "research_unfinished"
        else:
            reference = outcome.assessment_ref
            rationale = (
                f"{decision} モデル観測・一次資料によって企業の利益・普通株に帰属する資金の見通し、"
                "価格条件、または次に確かめる事実の何が変わったかを記す。変わらない場合も区別する。"
            )
            message = "行動・具体条件・比較理由の執筆と独立検証が未了です。"
            code = "proposal_content_requires_review"
        previous = (
            previous_targets.get(outcome.ticker)
            if outcome.purpose == "new" and isinstance(previous_targets, dict)
            else None
        )
        previous_review: tuple[str, ...] = ()
        if isinstance(previous, dict) and previous.get("status") == "available":
            previous_review = (
                (
                    "前回の指値・対象日を今回へ流用せず、元提案全体への本人報告があっても約定・現保有や"
                    "今後の一律見送りと決めず、企業の根拠・観測価格・取引条件のどれが今回の"
                    "「買う・待つ・見送る」を変えたか（変化なしも含む）を記す。"
                ),
            )
        listing = context.listing_designations
        flags = (listing.designations_by_ticker or {}).get(outcome.ticker, ())
        listing_review: tuple[str, ...] = ()
        if flags and listing.page_received_at is not None:
            stamp = listing.page_received_at.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
            listing_review = (
                (
                    f"JPXの保存原本（{stamp}取得）に「{'、'.join(flags)}」の記載。"
                    "現在の取引所指定と会社開示を確認し、新しい買い手に何の権利が残り、いつ現金化できるかを"
                    "確かめる。指定だけから買付けの成立、廃止日または支払日を決めない。"
                ),
            )
            lookup = context.tender_filing_references
            refs = (
                ()
                if lookup is None
                else (lookup.references_by_ticker or {}).get(outcome.ticker, ())
            )
            if refs:
                # Different offers for the same target can coexist in the window.
                groups: dict[str, list[str]] = {}
                for ref in refs:
                    groups.setdefault(ref.registration_doc_id, []).append(
                        f"{ref.document_label}（{ref.doc_id}）"
                    )
                names = " ／ ".join(
                    f"起点{root}：{'、'.join(documents)}" for root, documents in groups.items()
                )
                listing_review += (
                    (
                        f"EDINETの保存一覧に{names}。各届出の提出者と対象会社を区別して本文を読み、"
                        "その後の訂正・撤回も確認する。結果報告書の存在は成立や新規取得者への入金の証明ではない。"
                    ),
                )
            elif lookup is not None and lookup.status == "as_of_history_unavailable":
                listing_review += (
                    (
                        "後日に日別一覧が更新され、この保存コピーから判断時点のEDINET書類は復元できない。"
                        "当時の取下げ・訂正を否定せず、当時の受領版と発行体の開示を確認する。"
                    ),
                )
            else:
                listing_review += (
                    (
                        "今回のEDINET保存一覧ではこの対象の関連書類を特定できない。"
                        "公開買付けがないとはせず、現在の提出書類・発行体開示を確認する。"
                    ),
                )
        items.append(
            DeferItem(
                ticker=outcome.ticker,
                action="defer",
                assessment_ref=reference,
                rationale=rationale,
                review_conditions=(
                    terms_review,
                    *previous_review,
                    *listing_review,
                    (
                        "相対強弱が理由なら、自己の絶対騰落と同じ端点の指数代理・自己除外同業を照合する。"
                        "市場倍率と地合い、別期間の勢いを分け、相対強弱だけでPmaxや売却を決めない。"
                        "全シグナルのコメントは要求しない。"
                    ),
                ),
                blockers=(
                    GateReason(
                        code=code,
                        message=message,
                    ),
                ),
                resume_conditions=("不足を確認して行動と理由を記述し、独立検証を行う。",),
            )
        )
    warnings = build_proposal_warnings(
        context.inputs,
        tuple(items),
        context.liquidity_context,
        assessments={identity: pair.document for identity, pair in context.assessments.items()},
    )
    proposal = TradeProposalDraft(
        proposal_id=proposal_id,
        as_of=now.astimezone(JST).date(),
        prepared_at=now,
        request_scope=context.request_scope,
        research_scope=context.research_scope,
        headline="提案の要点を記述してください。",
        comparison=f"{alternative} 選んだ行動と、それを変える次の価格または企業事実を記す。",
        forgone="採らなかった行動とその理由を記述してください。",
        market_context_assessment_id=context.market_context_assessment_id,
        inputs=context.inputs,
        items=tuple(items),
        review=ProposalContentReview(
            review_id=f"proposal-review-{uuid4()}",
            attempt=1,
            reviewer_identity="独立Reviewerのidentityを記述してください。",
            reviewed_at=now,
            conclusion="pass",
            draft_sha256="0" * 64,
        ),
        liquidity_context=context.liquidity_context,
        warnings=warnings.warnings,
        portfolio_exposure=warnings.portfolio_exposure,
    )
    return proposal.payload()
