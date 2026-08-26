---
title: "Baibai Loop docs portal"
summary: "やりたいことから思想、運用skill、静的contractの唯一の正本へ案内する入口。"
doc_type: portal
status: active
---

# Baibai Loop docs

このportalは「何を知りたいか」から唯一の正本へ案内する。**運用手順の正本は skill（`.agents/skills/`、1 運用 = 1 skill）**、referenceはartifact・式・error/warningの意味、DB constraint・engine model・public `--help`は厳密な機械契約を持つ。上位docへ下位仕様を複製しない。

日常運用は [`AGENTS.md`](../AGENTS.md) の「運用の入口（trigger → skill）」から skill を選んで始める。基盤の方法変更は skill を持たず、[`reference/estimate-calibration.md`](./reference/estimate-calibration.md) の運用契約に従う issue → PR delivery で進める。

## 読者別reading path

| やりたいこと | 最初に読む | 必要時に読む |
| --- | --- | --- |
| 買い候補・指値提案 | skill [`shortlist`](../.agents/skills/shortlist/SKILL.md) →（選択後）[`research`](../.agents/skills/research/SKILL.md) | [`thesis`](./reference/thesis.md)、[`bargain-assessment`](./reference/bargain-assessment.md)、[`screening-runtime`](./reference/screening-runtime.md) |
| 人間から注文結果を受け取った | skill [`ledger-record`](../.agents/skills/ledger-record/SKILL.md) | [`portfolio ledger`](./reference/portfolio-ledger.md) |
| 決算後に保有を見直す | skill [`holding-review`](../.agents/skills/holding-review/SKILL.md) | [`holding review`](./reference/holding-review.md)、[`thesis`](./reference/thesis.md) |
| 市場環境レポートを書く | skill [`macro-context`](../.agents/skills/macro-context/SKILL.md) | [`macro`](./reference/macro.md)、[`data-sources`](./reference/data-sources.md) |
| batch・store・障害対応 | skill [`ops-maintenance`](../.agents/skills/ops-maintenance/SKILL.md) | [`batch/OPERATIONS.md`](../batch/OPERATIONS.md)、[`python-foundation`](./reference/python-foundation.md) |
| 見積り方法を改善する | [`estimate calibration`](./reference/estimate-calibration.md) の運用契約 | 対象referenceとreports/ |
| CLI/modelを変更する | [`architecture`](./architecture.md) | 対象reference、[`python-foundation`](./reference/python-foundation.md) |
| 初めてrepoを触る | [`doctrine`](./doctrine.md) → [`architecture`](./architecture.md) | 対象skillとreference |

## Docs layers

| layer | 正本 | 答える質問 |
| --- | --- | --- |
| orientation | root [`README`](../README.md) | repoは何をし、どこから始めるか |
| doctrine/governance | [`doctrine`](./doctrine.md)、[`portfolio management`](./portfolio-management.md) | 何を優先し、何をしないか |
| **運用手順** | [`.agents/skills/`](../.agents/skills/) | 今のtriggerをどの順で・どこで停止して完了するか |
| reference | [`reference/`](./reference/) | artifact、式、error/warningの意味 |
| machine contract | DB constraint、engine model、public `--help` | field/type/enum/optionの厳密な形 |

`.agents/skills`がcanonicalで、`.claude/skills`は同じdirectoryへのrelative symlinkである。skillは手順・gate順・停止条件・既知のgotchaを持ち、schema・式・policyの別正本を作らない（referenceへ参照する）。

## 仕様の所有者

| 仕様 | 唯一の所有者 |
| --- | --- |
| 投資価値の優先順位、永久損失、5年評価、AI・人間・broker責務 | [`doctrine.md`](./doctrine.md) |
| 追加資金・注文額目安・資本warning | [`portfolio-management.md`](./portfolio-management.md) |
| trigger routing、session共通規約、委譲・外部文書の扱い | [`AGENTS.md`](../AGENTS.md) |
| 5 層モデル・4 役の判定基準、store authority、package / CLI が仕える工程、無人経路の停止条件 | [`architecture.md`](./architecture.md) |
| lake の publication contract（authority・manifest・version 語彙・fail-close） | [`reference/market-lake.md`](./reference/market-lake.md) |
| 各運用の手順・gate順・停止条件 | `.agents/skills/<name>/SKILL.md` |
| thesisの式、lineage、review binding | [`reference/thesis.md`](./reference/thesis.md) |
| 統合判断（lane横比較・購入方法・content review束縛） | [`reference/bargain-assessment.md`](./reference/bargain-assessment.md) |
| ledger event/snapshot/reconciliation | [`reference/portfolio-ledger.md`](./reference/portfolio-ledger.md) |
| holding action contract | [`reference/holding-review.md`](./reference/holding-review.md) |
| macro layer（L1 series/L2 reading/L3 report contract・深度契約・8レンズ） | [`reference/macro.md`](./reference/macro.md) |
| screening runtime・select判断境界 | [`reference/screening-runtime.md`](./reference/screening-runtime.md) |
| 較正contractと改善サイクルの運用契約 | [`reference/estimate-calibration.md`](./reference/estimate-calibration.md) |
| field/type/enum | DB constraint、engine model |
| CLI option/default | public `--help` |

## 変更時に併せて確認する

| 変更 | docs / contract |
| --- | --- |
| macro context/provider | `reference/macro.md`、`reference/data-sources.md`、skill `macro-context` |
| screening/selection/SQLite | `reference/screening-runtime.md`、architecture CLI表、skill `shortlist` |
| thesis/review/opportunity | `reference/thesis.md`、skill `research` |
| ledger/result/holding/outcome | `reference/portfolio-ledger.md`、`reference/holding-review.md`、skill `ledger-record` / `holding-review` |
| model/write-time validation/CI | `architecture.md` Development gates、`reference/python-foundation.md` |
| local skill | AGENTS skill表、canonical skill、Claude symlink、skill inventory gate |

## 文書共通contract

- active docは`title / summary / doc_type / status`を持つ（skillはfrontmatterのname / descriptionのみ）。`title`と`summary`はこのportalとreference indexの表に出る。
- model fieldを網羅転記せず、意味・計算式・設計理由だけを書く。
- 現状とWHYを現在形で書き、Issue/PR由来、旧→新の変遷、進捗TODOを成果物本文に混ぜない。
- `doctrine.md#vocabulary`、`doctrine.md#fact-analysis-separation`のanchorを維持する。
