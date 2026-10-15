---
title: "Baibai Loop docs portal"
summary: "目的別の入口と、文書ごとの正本の所在。"
doc_type: portal
status: active
---

# Baibai Loop docs

Valuation・Market Context・External Assessmentの意味は[domain-language](./domain-language.md#analysis-domains)、現行実装の責務は[architecture](./architecture.md#repository-map)、移行・受入の進捗は[Issue #1414](https://github.com/koumatsumoto/baibai-loop/issues/1414)が所有する。

## 初めて読むとき

[doctrine](./doctrine.md)で目的と判断原則を確認し、[domain-languageのDecision flow](./domain-language.md#decision-flow)で観測・候補・判断・確認済み取引の関係を掴む。構成や変更箇所を調べるときは[architecture](./architecture.md#repository-map)へ進む。実際の操作は下表の入口から始め、必要なreferenceだけを参照する。

## 目的別の入口

| 目的 | 入口 |
| --- | --- |
| 明示開始から有限調査・新規/保有提案を進める | [Research](../.agents/skills/research/SKILL.md) |
| 注文結果・入出金を記録する | [Ledger Record](../.agents/skills/ledger-record/SKILL.md) |
| 保有銘柄を見直す | [Researchの保有見直し](../.agents/skills/research/SKILL.md) |
| 保存済み外部参考評価を読む | [External Assessmentの意味](./reference/data-sources.md#external-assessment)・[保存済み照会](./reference/market-lake.md#external-assessment-query) |
| 保存済み市場・業種・個別相対を読む | [Market Context読取](./reference/market-context.md#market-context-reads) |
| 市場環境判断を作る | [Market Context assessment](../.agents/skills/market-context-assessment/SKILL.md) |
| batch・storeを運用する | [Ops Maintenance](../.agents/skills/ops-maintenance/SKILL.md) |
| 保存済み情報を表示・照会する | [Web](../web/README.md)・[固定lake release読取](./reference/market-lake.md#fixed-release-read) |
| 保存済みの機械評価と企業固有評価を読む | [Valuation metrics](./reference/valuation-metrics.md#machine-valuation)・[Stock Assessment](./reference/stock-assessment.md#scenario-arithmetic) |
| 見積り方法・保存済みER較正文脈を確認する | [Estimate calibration](./reference/estimate-calibration.md) |
| 条件・閾値の目的と根拠を確認する | [判断条件の見取り図](#判断条件の見取り図)から各責務の正本へ辿る |
| PC・クラウドで開発する、Actionsとの分担を確認する | [AGENTS](../AGENTS.md)・[Python foundation](./reference/python-foundation.md#cloud-development-boundary) |
| artifact・指標の意味を調べる | [Reference index](./reference/README.md) |

## 判断条件の見取り図

[取得・観測の利用可能性](./reference/data-sources.md)は銘柄の価値判断と分ける。計算できるか、候補として調べるか、購入するか、保有するかも別の問いである。各条件の数値・判定式は対応する実装とmethodが所有し、下表は理由と責務への入口だけを示す。閾値の採否・未測定事項の進捗は[Issue #1481](https://github.com/koumatsumoto/baibai-loop/issues/1481)に残し、テスト成功を経済的な最適性とは扱わない。

| 判断の段階 | 理由・意味の正本 | 実装と代表的な回帰 |
| --- | --- | --- |
| 計算入力から候補集合・候補一覧まで | [指標と欠測](./reference/valuation-metrics.md)、[Screeningの資格・順位・品質](./reference/screening-runtime.md#rule-purpose) | [`CommonEligibilityRules.matches`](../engine/src/baibai_engine/screening/rule_config.py)、[候補文脈](../engine/src/baibai_engine/screening/discovery/candidates.py)、[回帰](../tests/engine/test_candidate_discovery.py) |
| 機械E[r]の参考値と較正 | [機械見積り](./reference/valuation-metrics.md#machine-valuation)、[較正文脈](./reference/estimate-calibration.md#estimator-vs-decision) | [文脈読取](../engine/src/baibai_engine/valuation/calibration/query.py)、[回帰](../tests/engine/test_er_calibration_context_contract.py) |
| 明示Researchと企業固有の評価 | [有限対象と進行](./reference/analysis-operations.md)、[Stock Assessmentと独立Review](./reference/stock-assessment.md) | [対象と進捗](../engine/src/baibai_engine/research/operation/progress.py)、[回帰](../tests/engine/test_research_progression.py) |
| 候補比較と新規購入 | [Investment Proposal](./reference/investment-proposal.md)、[資本方針](./portfolio-management.md) | [`evaluate_buy_terms`](../engine/src/baibai_engine/research/entry_policy.py)、[回帰](../tests/engine/test_conditional_buy_terms.py) |
| 保有継続・回収と確認済み取引 | [保有提案](./reference/investment-proposal.md#holding)、[Portfolio Ledger](./reference/portfolio-ledger.md) | [保有判断](../engine/src/baibai_engine/research/holding_policy.py)、[台帳回帰](../tests/engine/test_portfolio_ledger.py) |

## 文書の役割

| 正本 | 所有する情報 |
| --- | --- |
| [doctrine](./doctrine.md) | 投資目的、判断原則、開発・設計・レビューの原則、非目標 |
| [portfolio-management](./portfolio-management.md) | 資本・配分・保有の方針 |
| [domain-language](./domain-language.md) | 正準用語、成果物の関係、命名 |
| [architecture](./architecture.md) | packageの責務・依存、store authority、無人経路の停止方針 |
| [AGENTS](../AGENTS.md) | repository共通の作業・提出規約 |
| [skills](../.agents/skills/)・各OPERATIONS | 操作の開始条件、順序、結果別の次の行動、復旧 |
| [reference](./reference/README.md) | artifact・指標・判断入力の意味と解釈上の制約 |
| model・DB schema・public `--help` | field、type、enum、option、機械検証の厳密な形 |
| [anti-patterns](./anti-patterns.md) | 過去に取り違えた論点から正本へ辿る索引 |
| [method](../method/README.md)・[reports](../reports/README.md) | 採用methodと、当時の測定・判断の記録 |

<a id="document-writing-contract"></a>

## 文書の共通規約

規則・操作・定義は正本1か所に集約し、他の文書は直接参照する。追記の前に不要な説明を削除し、必要な情報は既存記述へ統合・置換する。手順には実行例、必要な前提、成功確認、失敗時の行き先を残す。

コードが所有するfield・validator・CLI optionの網羅表、一般論、未採用の将来案は複写しない。判断文書の編集は[judgment-writing](./reference/judgment-writing.md)に従う。

active文書は現在の契約を記す。dated method・実取引・historical evidenceは書き換えず、frontmatterと索引は変更した所有範囲に合わせる。
