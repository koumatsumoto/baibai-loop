---
title: "Baibai-Loop docs portal"
summary: "やりたいことから思想、運用、工程、contract、AI skillの唯一の正本へ案内する入口。"
doc_type: portal
status: active
last_reviewed: 2026-07-14
---

# Baibai-Loop docs

このportalは「何を知りたいか」から唯一の正本へ案内する。operationはいつ・どの順に動くか、workflowは各工程が入力を成果物へどう変換するか、referenceはartifact・式・errorの意味、DB constraint・engine model・public `--help`は厳密な機械契約を持つ。上位docへ下位仕様を複製しない。

日常運用は[`operations/decision-cycle.md`](./operations/decision-cycle.md)から始める。個別workflow/referenceから読み始めてtrigger、人間境界、記録先を推測しない。基盤の方法変更は[`operations/improvement-loop.md`](./operations/improvement-loop.md)から始め、個別銘柄判断と混ぜない。

## 読者別reading path

| やりたいこと | 最初に読む | 必要時に読む | 通常読まない |
| --- | --- | --- | --- |
| 買い候補・指値提案 | [`decision-cycle`](./operations/decision-cycle.md) → [`decision-cycle skill`](../.agents/skills/decision-cycle/SKILL.md) | [`screening`](./workflow/screening.md)、[`research`](./workflow/research.md)、[`decision packet`](./reference/decision-packet.md) | improvement-loop、src、fixtures |
| 人間から注文結果を受け取った | [`decision-cycle#human-result-path`](./operations/decision-cycle.md#human-result-path) | [`position`](./workflow/position.md)、[`portfolio ledger`](./reference/portfolio-ledger.md) | screening、macro |
| 決算後に保有を見直す | [`decision-cycle#earnings-and-material-event-path`](./operations/decision-cycle.md#earnings-and-material-event-path) | [`research`](./workflow/research.md)、[`position`](./workflow/position.md)、[`holding review`](./reference/holding-review.md) | 全銘柄screening |
| 年次結果を確認する | [`decision-cycle#annual-outcome-path`](./operations/decision-cycle.md#annual-outcome-path) | [`position`](./workflow/position.md)、[`estimate calibration`](./reference/estimate-calibration.md) | 新規候補selection |
| 見積り方法を改善する | [`improvement-loop`](./operations/improvement-loop.md) | [`estimate calibration`](./reference/estimate-calibration.md)、対象workflow | 個別proposal手順 |
| CLI/modelを変更する | [`architecture`](./architecture.md) | 対象reference、[`testing`](./reference/testing-and-validation.md) | completed operation session |
| 初めてrepoを触る | [`doctrine`](./doctrine.md) → [`architecture`](./architecture.md) | 対象cycleとlocal skill | 全records |

## Docs layers

| layer | 正本 | 答える質問 |
| --- | --- | --- |
| orientation | root [`README`](../README.md) | repoは何をし、どこから始めるか |
| doctrine/governance | [`doctrine`](./doctrine.md)、[`portfolio management`](./portfolio-management.md) | 何を優先し、何をしないか |
| operations | [`operations/`](./operations/) | 今のtriggerをどの順で完了するか |
| workflow | [`workflow/`](./workflow/) | 工程がinputをoutputへどう変換するか |
| reference | [`reference/`](./reference/) | artifact、式、error/warningの意味 |
| machine contract | DB constraint、engine model、public `--help` | field/type/enum/optionの厳密な形 |
| AI execution | [`.agents/skills/`](../.agents/skills/) | この依頼で何を読み、どこまで実行するか |

`.agents/skills`がcanonicalで、`.claude/skills`は同じdirectoryへのrelative symlinkである。skillはtriggerとroutingだけを持ち、policy、schema、完全手順の別正本を作らない。

## 仕様の所有者

| 仕様 | 唯一の所有者 |
| --- | --- |
| 投資価値の優先順位、永久損失、5年評価 | [`doctrine.md`](./doctrine.md) |
| 追加資金・注文額目安・資本warning | [`portfolio-management.md`](./portfolio-management.md) |
| AI・人間・broker責務 | [`doctrine.md`](./doctrine.md) |
| trigger、e2e順序、operation checkpoint | [`operations/decision-cycle.md`](./operations/decision-cycle.md) |
| subsystem input/output/failure | 各[`workflow`](./workflow/) |
| packetの式、lineage、review binding | [`reference/decision-packet.md`](./reference/decision-packet.md) |
| ledger event/snapshot/reconciliation | [`reference/portfolio-ledger.md`](./reference/portfolio-ledger.md) |
| holding action contract | [`reference/holding-review.md`](./reference/holding-review.md) |
| field/type/enum | DB constraint、engine model |
| CLI option/default | public `--help` |
| skill trigger/routing | `.agents/skills/<name>/SKILL.md` |

非所有docは1〜2文の意味とrelative linkだけを持つ。日常運用の完全command recipeはdecision-cycle、subsystemのfailure semanticsはworkflow、式とcontract意味はreferenceに置く。

## 変更時に併せて確認する

| 変更 | docs / contract |
| --- | --- |
| macro context/provider | `workflow/macro.md`、`reference/data-sources.md` |
| screening/selection/SQLite | `workflow/screening.md`、`reference/screening-runtime.md`、architecture CLI表 |
| decision packet/review/opportunity | `workflow/research.md`、`reference/decision-packet.md`、decision-cycle recipe |
| ledger/result/holding/outcome | `workflow/position.md`、対応reference、decision-cycle human-result/holding節 |
| model/write-time validation/CI | `reference/testing-and-validation.md`、`reference/python-foundation.md` |
| local skill | AGENTS skill表、canonical skill、Claude symlink、skill inventory gate |

## 文書共通contract

- active docは`title / summary / doc_type / status / last_reviewed`を持つ。
- `last_reviewed`は本文、link、public CLIを実際に照合した日。
- 実行手順を持つleaf operation/workflow/referenceは、適用可能なfailure、validation、relatedを末尾に置く。portal/index、思想文書、用語・式だけのreferenceは適用外である。
- model fieldを網羅転記せず、意味・計算式・設計理由だけを書く。
- 現状とWHYを現在形で書き、Issue/PR由来、旧→新の変遷、進捗TODOを成果物本文に混ぜない。
- `doctrine.md#vocabulary`、`doctrine.md#fact-analysis-separation`とdecision-cycleの主要anchorを維持する。
