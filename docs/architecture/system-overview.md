---
title: "System overview"
summary: "Current Baibai-Loop architecture: four core records, two downstream records, macro and micro tracks, and explicit non-goals."
doc_type: architecture
status: active
last_reviewed: 2026-05-04
related_docs:
  - "../philosophy.md"
  - "../design-principles.md"
  - "information-flow.md"
---

# System overview

Baibai-Loop は、日本株スイングトレードの判断を forward-only に記録し、改善するための decision-support 基盤です。構造は **4 成分 + 下流 2 成分** を正本とします。

| 成分 | directory | レイヤー | 役割 |
| --- | --- | --- | --- |
| a | `records/01-brief/` | マクロ事実 | 一次統計、地政学、マーケット指標を事実として蓄積する |
| b | `records/03-candidates/` | ミクロ事実 | universe と valuation 条件で機械的に残った銘柄を記録する |
| c | `records/02-outlook/` | マクロ分析 | brief を積み上げ、業種・地域の tailwind / neutral / headwind を判断する |
| d | `records/04-research/` | ミクロ分析 | candidates と outlook を統合し、個別銘柄の採用判定を残す |
| downstream | `records/05-trades/` | 執行 | 採用された research packet の entry / exit / position を記録する |
| downstream | `records/06-reviews/` | 検証 | 決済後 review と月次 retro で feedback loop を閉じる |

## 2 トラック

Macro track は売買イベントと独立して更新します。

```text
records/01-brief/ -> records/02-outlook/
```

Micro track は売買ループと連動します。

```text
records/03-candidates/ -> records/04-research/ -> records/05-trades/ -> records/06-reviews/
```

統合点は `records/04-research/` です。research は最新 candidates と最新 outlook を入力にし、Macro gate を通過しない銘柄を採用しません。この設計は [`../philosophy.md`](../philosophy.md) の「マクロ優位 (76/24)」と [`../design-principles.md`](../design-principles.md) の「事実と分析の分離」を具体化したものです。

## スコープ

- 基本は 2 か月以内、5-40 営業日のスイングトレードを対象にする。
- long-only の裁量支援基盤として扱う。
- トレード判断の比重はマクロ 76% / ミクロ 24% とし、運用途中で動かさない。
- Markdown / YAML と Git を正本にする。
- AI 下書きと人間確認を前提に、事実層と分析層を物理的に分ける。
- CLI は screening、validation、ledger sync の補助に使う。
- ledger と reviews は forward-only な検証証跡として扱う。

## 非目標

- バックテスト、累積リターン計算、パラメータ最適化は行わない。
- 自動発注は行わない。
- screening 閾値や playbook を過去データに fit させない。
- 配当利回り / Rerating Book は対象外にする。
- SQLite / feature store / BI 基盤を先行導入しない。

詳細な rationale は [`../philosophy.md`](../philosophy.md)、実践ルールは [`../design-principles.md`](../design-principles.md)、成果物ごとの contract は [`../components/README.md`](../components/README.md) を参照します。
