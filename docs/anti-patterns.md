---
title: "Anti-patterns"
summary: "過去に取り違えた論点から、現在の正本へ辿る索引。"
doc_type: governance
status: active
---

# Anti-patterns

変更領域に関係する論点から正本を確認する。ここでは規則・validator・test caseを再定義しない。個別事故の経緯はGit historyと該当Issueに残す。

## 1. AP-01: 一次情報を直接確認せず二次情報・推測で書く

一次値、二次配信、未確認の区別は[data sources](./reference/data-sources.md)。判断文書を要約・編集する際の意味保全は[judgment-writing](./reference/judgment-writing.md)。

## 2. AP-02: 数値計算を機械的に検算しない

購入価格・見返りの算術は[Thesis](./reference/thesis.md)、資金移動と運用成績は[Portfolio Ledger](./reference/portfolio-ledger.md)。別の計算式をchecklistへ持たない。

## 3. AP-03: データの異常を経済的な変化と即断する

株式分割・価格・会計期間のbasisは[valuation metrics](./reference/valuation-metrics.md)、未解決exitは[calibration](./reference/estimate-calibration.md)。

## 4. AP-04: field名から意味を推測する

artifactの意味は[reference index](./reference/README.md)、形は対応model・schema。旧field名と現行の意味を混同しない。

## 5. AP-05: 機械見積りと判断を混同する

[doctrineのfact/analysis分離](./doctrine.md#fact-analysis-separation)を参照する。

## 6. AP-06: Macroの層と参照方向を混同する

観測・Reading・Context・connectionの役割は[macro](./reference/macro.md)。

## 7. AP-07: 公表日と観測時点を混同する

[時刻の語彙](./domain-language.md#time-vocabulary)と[macro](./reference/macro.md)を参照する。公表予定の推定値を公式event日程として扱わない。

## 8. AP-08: 保存・発行境界を別の入口から迂回する

変更するdomainのwriter・reader・serviceと対応testを確認する。対象集合と発行は[Screening](./reference/screening-runtime.md)、[Thesis](./reference/thesis.md)、[CAA](./reference/capital-allocation-assessment.md)。取引事実は[Portfolio Ledger](./reference/portfolio-ledger.md)、転送・競合・部分失敗は[batch運用](../batch/OPERATIONS.md)。

## 9. AP-09: 企業評価・配分・取引事実を一つの判断にする

[Decision flow](./domain-language.md#decision-flow)と各artifactのreferenceを参照する。現在の購入適格性と、既に行われた取引の記録は別の境界である。

## 10. AP-10: 共通処理を別実装し、性能を推測する

YAML読込は`baibai_engine.foundation.yaml_io`、検証と性能変更の扱いは[Python foundation](./reference/python-foundation.md)。

## 11. AP-11: 観測頻度と公表・取得頻度を同一視する

鮮度と実効窓は[macro](./reference/macro.md#macro-reading)、信用残高の公表日基準は[移行契約](./reference/margin-publication-transition.md)。

## 12. AP-12: 資本・株式・実体・期間の基準を混ぜる

会計・株式basisは[valuation metrics](./reference/valuation-metrics.md)、将来価値と分配は[Thesis](./reference/thesis.md)、実取引とNAVは[Portfolio Ledger](./reference/portfolio-ledger.md)。同じ「一株値」でも対応する株式数と期間は同じとは限らない。

## 13. AP-13: 検出できない状態を対象なしと読む

sourceが返さない値、writerが書かない値、実際に対象がない場合を区別する。該当provider・writer・readerと、その契約を検証するtestを確認する。

## 14. AP-14: 同じ契約の別の説明を置き去りにする

[文書の共通規約](./README.md#document-writing-contract)に従い、正本を更新して参照元を揃える。規則の複写で再発を防ごうとしない。
