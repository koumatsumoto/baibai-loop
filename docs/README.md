---
title: "Baibai Loop docs portal"
summary: "目的別の入口と、文書ごとの正本の所在。"
doc_type: portal
status: active
---

# Baibai Loop docs

## 目的別の入口

| 目的 | 入口 |
| --- | --- |
| 買い候補を探す | [Research Triage](../.agents/skills/research-triage/SKILL.md) |
| 選んだ企業を調査して配分を判断する | [Research](../.agents/skills/research/SKILL.md) |
| 注文結果・入出金を記録する | [Ledger Record](../.agents/skills/ledger-record/SKILL.md) |
| 保有銘柄を見直す | [Position Review](../.agents/skills/position-review/SKILL.md) |
| Macro Contextを書く | [Macro Context](../.agents/skills/macro-context/SKILL.md) |
| 外部ChatのMacro draftを受け渡す | [Macro handoff](./reference/macro-handoff.md) |
| batch・storeを運用する | [Ops Maintenance](../.agents/skills/ops-maintenance/SKILL.md) |
| 保存済み情報を表示・照会する | [Web](../web/README.md)・[Owner MCP](../tools/owner_mcp/README.md) |
| 見積り方法を改善する | [Estimate calibration](./reference/estimate-calibration.md) |
| 開発する | [AGENTS](../AGENTS.md)・[Python foundation](./reference/python-foundation.md) |
| artifact・指標の意味を調べる | [Reference index](./reference/README.md) |

## 文書の役割

| 正本 | 所有する情報 |
| --- | --- |
| [doctrine](./doctrine.md) | 投資目的、判断原則、改善の価値基準、非目標 |
| [portfolio-management](./portfolio-management.md) | 資本・配分・保有の方針 |
| [domain-language](./domain-language.md) | 正準用語、成果物の関係、命名 |
| [architecture](./architecture.md) | packageの責務・依存、store authority、無人経路の停止方針 |
| [AGENTS](../AGENTS.md) | repository共通の作業・提出規約 |
| [skills](../.agents/skills/)・各OPERATIONS | 操作の開始条件、順序、結果別の次の行動、復旧 |
| [reference](./reference/README.md) | artifact・指標・判断入力の意味と解釈上の制約 |
| model・DB schema・public `--help` | field、type、enum、option、機械検証の厳密な形 |
| [anti-patterns](./anti-patterns.md) | 過去に取り違えた論点から正本へ辿る索引 |
| [method](../method/README.md)・[reports](../reports/README.md) | 採用methodと、当時の測定・判断の記録 |

`.agents/skills/<name>/SKILL.md`がskill本文の正本で、`.claude/skills/<name>`は対応するrelative symlinkである。

<a id="document-writing-contract"></a>

## 文書の共通規約

規則・操作・定義の本文は一か所に置き、他の文書は案内に留める。入口から操作の所有者へ直接辿れるようにする。手順の正本には実行例、必要な前提、成功確認、失敗時の行き先を残す。

コードから分かる全field・validator・CLI optionの列挙、一般的な実装や文章の心得、採用していない将来案を常設文書へ追加しない。判断内容の編集は[judgment-writing](./reference/judgment-writing.md)に従う。

active文書は現在の契約を記す。dated method、実取引記録、historical evidenceは文体整理で書き換えない。`docs/`のfrontmatterとreference indexは本文の所有範囲に合わせる。
