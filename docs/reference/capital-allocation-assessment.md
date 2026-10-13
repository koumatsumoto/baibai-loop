---
title: "Capital Allocation Assessment"
summary: "企業評価を横比較した配分判断と、content reviewの意味。"
doc_type: reference
status: active
---

# Capital Allocation Assessment

明示開始したOperationで完了した新規対象のReviewed Thesisを比較し、`allocate / no_allocation / defer`を固定する判断である。企業ごとの根拠はThesisとThesis Review、集合全体の比較と見送り理由はこのAssessmentが所有する。

## 判断の内容

allocateは一つのalternativeを選び、そのThesisとReviewへ束縛する。no_allocationとdeferは選択対象を持たない。完了した経済的reject・unresolvedは比較できる。調査未完了と保有提案はalternativesへ混ぜず、配分対象は現在の購入条件を満たす必要がある。

`comparison / forgone / alternatives[].rationale`には、[投資目的とリスク許容](../doctrine.md#investment-strategy)に沿って、重要な成立条件、確からしさを支える根拠と反証、実現までの時間、上振れ・下振れの見返りを記す。同じBase年率でも倍率上昇と業績回復への依存を分け、Pmax内という理由だけで配分しない。詳しい算術の読み方は[Thesis](./thesis.md#valuation-context)を参照する。

## 保存とReview

Assessmentは比較理由と判断bindingを保存する。Thesis由来の機械値、当日の指値・数量・期限は複写しない。`review`はAssessmentの内容digestに対するreviewであり、個別企業のThesis Reviewとは別である。厳密なfield・version・拒否条件はmodelとpublic scaffoldが所有する。

同じID・内容・Reviewの再送は既存publicationを返し、異なる内容で同じIDを上書きしない。開始したOperationの対象集合と無関係なAssessmentで完了させない。現行writerの`research_scope`はOperation IDと新規の完了/未完了被覆へ束縛され、alternativesは完了Reviewed ThesisのID/core hash/Review参照と一致する。未完了の理由・参照も同じCAAに固定する。全未完了ならCAAを作らない。L3完了にはいずれの配分結果でも人間の採否を要求しない。旧payloadのscope欠如は履歴として読み、新しい発行に補完しない。作成・check・content review・公開・完了の順序は[Research skill](../../.agents/skills/research/SKILL.md)に従う。

## Planningと資本情報

Planningは公開済みallocateから当日の価格・数量・期限を計算する一時的な助言であり、Assessment自体の変更ではない。broker操作と確認済み事実の保存は別工程である。

`portfolio_exposure.common_factor_unclassified_tickers`が非空の場合、既知factorの比率は下限である。自動分類や新しい停止条件を設けず、表示されるcoverage warningと既知factorの集中warningを区別して読む。
