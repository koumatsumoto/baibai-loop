---
title: "Baibai Loop docs portal"
summary: "目的別の入口、文書の所有者、active documentationの共通規約を示す正本。"
doc_type: portal
status: active
---

# Baibai Loop docs

このportalは、読者の目的から一意な正本へ案内します。運用手順は[`.agents/skills/`](../.agents/skills/)、artifact・式・warningの意味は[`reference/`](./reference/)、field・enumはDB constraintとengine model、CLI optionはpublic `--help`が所有します。

## 目的別の入口

| やりたいこと | 最初に読む | 詳細 |
| --- | --- | --- |
| 買い候補を探す | skill [`research_triage`](../.agents/skills/research-triage/SKILL.md) | 人間の選択後は[`research`](../.agents/skills/research/SKILL.md) |
| 注文結果を記録する | skill [`ledger-record`](../.agents/skills/ledger-record/SKILL.md) | [`portfolio-ledger.md`](./reference/portfolio-ledger.md) |
| 保有銘柄を見直す | skill [`position-review`](../.agents/skills/position-review/SKILL.md) | [`position-review.md`](./reference/position-review.md) |
| Macro Contextを書く | skill [`macro-context`](../.agents/skills/macro-context/SKILL.md) | [`macro.md`](./reference/macro.md) |
| batch・storeを運用する | skill [`ops-maintenance`](../.agents/skills/ops-maintenance/SKILL.md) | [`batch/OPERATIONS.md`](../batch/OPERATIONS.md) |
| 見積り方法を改善する | [`estimate-calibration.md`](./reference/estimate-calibration.md) | 対象referenceとhistorical study |
| 開発を始める | [`AGENTS.md`](../AGENTS.md) | [`architecture.md`](./architecture.md)と対象module README |

## 文書の役割

| 種類 | 答える質問 | 正本 |
| --- | --- | --- |
| orientation | repositoryは何をし、どこから始めるか | root [`README.md`](../README.md) |
| doctrine / governance | 何を優先し、何をしないか | [`doctrine.md`](./doctrine.md)、[`portfolio-management.md`](./portfolio-management.md) |
| domain language | artifact・activity・state・methodを何と呼ぶか | [`domain-language.md`](./domain-language.md) |
| agent rule | 作業開始・停止・提出をどう進めるか | [`AGENTS.md`](../AGENTS.md) |
| operation | triggerをどの順で進め、どこで停止するか | [`.agents/skills/`](../.agents/skills/) |
| architecture | layer・責務・依存・physical authority・無人経路の停止条件 | [`architecture.md`](./architecture.md)、[`failure policy`](./architecture.md#failure-policy) |
| reference | artifact・式・artifact固有のerror / warningの意味 | [`reference/README.md`](./reference/README.md) |
| review checklist | 過去のfailure classと変更対象別のcommit前停止条件 | [`anti-patterns.md`](./anti-patterns.md) |
| machine contract | field・type・enum・optionの厳密な形 | DB constraint、engine model、public `--help` |
| historical evidence | 当時の入力・判断・結果 | `reports/studies/`、`reports/operations/` |

`.agents/skills`がskillの正本です。`.claude/skills`は同じdirectoryへのrelative symlinkであり、別文書として編集・計上しません。L3判断文書の文章契約は[`judgment-writing.md`](./reference/judgment-writing.md)が所有します。

## 変更時の確認先

| 変更 | 同時に確認する正本 |
| --- | --- |
| domain-facing term / identifier | `domain-language.md`、対象artifactのreference |
| macro context / provider | `reference/macro.md`、`reference/data-sources.md`、skill `macro-context` |
| screening / Candidate Discovery / SQLite | `reference/screening-runtime.md`、skill `research-triage`、architectureのCLI表 |
| thesis / review / capital allocation | `reference/thesis.md`、`reference/capital-allocation-assessment.md`、skill `research` |
| ledger / holding / outcome | `reference/portfolio-ledger.md`、`reference/position-review.md`、対応skill |
| model / write-time validation / CI | `architecture.md`、`reference/python-foundation.md`、変更domainに対応する`anti-patterns.md`の`AP-*` |
| local skill | `AGENTS.md`のskill表、`.agents/skills`、Claude symlink、skill inventory gate |

<a id="document-writing-contract"></a>

## Active documentationの共通規約

この節はrepositoryの一般documentationだけを所有します。判断内容の文章規約は[`judgment-writing.md`](./reference/judgment-writing.md)、domain field固有の意味は各referenceを正本とします。

### 1. 一つの主要な読者質問と一意な所有者

一つの文書は一つの主要な読者質問を持ちます。関連する複数契約を所有してもよいですが、同じ契約を二つの文書が正本として主張してはいけません。

### 2. 答え・規則・操作を先に置く

現在の結論、守る規則、実行する操作、停止条件を先に置きます。理由、例外、補足は後に置き、履歴は分離します。

### 3. 一段落一役割・一文一命題

規則、理由、例、例外、手順を一段落へ混ぜません。機械的な短文化や文長制限は行いません。

### 4. 条件・主体・結果を明示する

必須、禁止、許可、既定、警告、停止、失敗結果を言い分けます。指示語や主語のない受動態に依存しません。

### 5. 日本語説明とidentifierを分ける

自然文は日本語で書きます。command、field、enum、ID、path、class、function、`Research Triage`や`Thesis`などの正準artifact名はcode表記を維持します。日本語化率は目的にしません。

### 6. 情報の型に合う構造を使う

- 一行一比較軸に収まる並列情報はtableにします。
- 条件分岐・優先順位・例外は見出しと箇条書きまたは散文で示します。
- 順序操作は番号付き手順、切り戻しと復旧は手順として示します。
- 因果と得失は散文、commandと式はcode blockにします。

散文をtableへ移しただけの変更は改善と数えません。

### 7. 重複よりlink、ただし行動地点は局所完結

厳密な契約は正本へ置き、他文書は必要なcontextとlinkだけを持ちます。ただし操作地点には、前提、危険、成功確認、停止、rollbackを残します。link先を開かないと誤操作を止められない構造は禁止します。

### 8. Active instructionとhistorical evidenceを分ける

active docは現在の契約だけを現在形で書きます。過去判断・事故・旧仕様は履歴へ分離してlinkし、dated methodとhistorical evidenceを読みやすさのために書き換えません。

### 9. 意味と機械契約を保全する

削除前に、数値、ID、式、field、enum、条件、例外、停止、authority、human boundary、anchor、linkを固定します。機械の読み手を確認し、command、placeholder、default、required、choice、usage、exit semanticsを文章整理で変えてはいけません。

## Active docのlifecycle

`docs/**.md`のactive文書はfrontmatterに`title`、`summary`、`doc_type`、`status`を持ちます。所有範囲を変えた場合は本文、frontmatter、portalまたはreference indexを同時に更新します。運用sessionの進捗、Issue固有の作業履歴、旧→新の変遷はactive文書へ置きません。
