---
title: "Baibai Loop docs portal"
summary: "目的別の入口と、文書ごとの正本の所在。"
doc_type: portal
status: active
---

# Baibai Loop docs

Valuation・Market Context・External Assessmentの意味は[domain-language](./domain-language.md#analysis-domains)、現行実装の責務と移行状況は[architecture](./architecture.md#repository-map)が所有する。

## 初めて読むとき

[doctrine](./doctrine.md)で目的と判断原則を確認し、[domain-languageのDecision flow](./domain-language.md#decision-flow)で観測・候補・判断・確認済み取引の関係を掴む。構成や変更箇所を調べるときは[architecture](./architecture.md#repository-map)へ進む。実際の操作は下表の入口から始め、必要なreferenceだけを参照する。

## 目的別の入口

| 目的 | 入口 |
| --- | --- |
| 保存済みReview SetをResearch Triageで判断する | [Research Triage](../.agents/skills/research-triage/SKILL.md) |
| 明示開始から有限調査・新規/保有提案を進める | [Research](../.agents/skills/research/SKILL.md) |
| 注文結果・入出金を記録する | [Ledger Record](../.agents/skills/ledger-record/SKILL.md) |
| 保有銘柄を見直す | [Position Review](../.agents/skills/position-review/SKILL.md) |
| 保存済み外部参考評価を読む | [External Assessmentの意味](./reference/data-sources.md#external-assessment)・[保存済み照会](./reference/market-lake.md#external-assessment-query) |
| 保存済み市場・業種・個別相対を読む | [Market Context読取](./reference/macro.md#market-context-reads) |
| Macro Contextを書く | [Macro Context](../.agents/skills/macro-context/SKILL.md) |
| 外部ChatのMacro draftを受け渡す | [Macro handoff](./reference/macro-handoff.md) |
| batch・storeを運用する | [Ops Maintenance](../.agents/skills/ops-maintenance/SKILL.md) |
| 保存済み情報を表示・照会する | [Web](../web/README.md)・[Owner MCP](../tools/owner_mcp/README.md) |
| 保存済みの機械評価と企業固有評価を読む | [Valuation metrics](./reference/valuation-metrics.md#machine-valuation)・[Thesis](./reference/thesis.md#scenario-arithmetic) |
| 見積り方法・保存済みER較正文脈を確認する | [Estimate calibration](./reference/estimate-calibration.md) |
| PC・クラウドで開発する、Actionsとの分担を確認する | [AGENTS](../AGENTS.md)・[Python foundation](./reference/python-foundation.md#cloud-development-boundary) |
| artifact・指標の意味を調べる | [Reference index](./reference/README.md) |

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
