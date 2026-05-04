---
title: "Anti-pattern governance"
summary: "Governance entry point for maintaining the retained canonical anti-pattern checklist."
doc_type: governance
status: active
last_reviewed: 2026-05-04
related_docs:
  - "../anti-patterns.md"
---

# Anti-pattern governance

失敗パターンと commit / PR 前 checklist の正本は [`../anti-patterns.md`](../anti-patterns.md) です。この file は governance 側の入口であり、本文を重複させません。

## Update rule

- 同じ failure mode を 2 回以上 PR review で指摘されたら、[`../anti-patterns.md`](../anti-patterns.md) の該当節を強化します。
- 新 validator rule を追加するときは、[`../anti-patterns.md`](../anti-patterns.md) AP-08 の checklist を確認します。
- docs PR では anti-pattern の参照 path と節番号を壊していないか確認します。
