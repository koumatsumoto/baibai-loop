---
title: "Workflow"
summary: "単一ループの各工程（macro → screening → research → position）と playbooks の入口。1 工程 = 1 doc。"
doc_type: workflow
status: active
last_reviewed: 2026-07-02
---

# Workflow — 単一ループの各工程

[`../doctrine.md`](../doctrine.md) §2 の単一ループを、1 工程 = 1 doc で辿る。各 doc は概念の説明・手順・最小限の例を持ち、成果物の機械契約は `records/_schemas/*.json` を正本にする。

```mermaid
flowchart LR
  macro["macro.md<br/>姿勢・セクター・AI 前提"] --> screening["screening.md<br/>割安 ranking → candidates"]
  screening --> research["research.md<br/>FV・RR・期待利回り・耐性"]
  research --> position["position.md<br/>買い・長期保有・全売り・calibration"]
  position -.見積り calibration.-> macro
  playbooks["playbooks.md<br/>割安 value archetype"] -.参照.-> research
```

| 工程 | doc | 役割 |
| --- | --- | --- |
| マクロ環境分析 | [`macro.md`](./macro.md) | 指標を引き、姿勢（ディフェンシブ / リスクオン）・セクター・AI 前提を読む |
| 割安 screening | [`screening.md`](./screening.md) | 全上場銘柄から割安ゾーンを機械抽出し、candidatesのobserved / derived / estimateを出す |
| 個別銘柄リサーチ | [`research.md`](./research.md) | FV・RR・期待利回りを見積もり、塩漬け耐性を確認して採否と投入額を決める |
| 執行・保有 | [`position.md`](./position.md) | 注文・約定・長期保有・押し目買増し・割高で全売り・見積り calibration |
| 戦略プレイブック | [`playbooks.md`](./playbooks.md) | research が参照する割安 value の archetype |

上流の運用方針は [`../portfolio-management.md`](../portfolio-management.md)、構造は [`../architecture.md`](../architecture.md)、詳細な参照仕様は [`../reference/`](../reference/) を見る。
