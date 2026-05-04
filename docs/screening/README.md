---
title: "Screening subsystem"
summary: "Index and role map for the screening subsystem docs. Principles remain canonical in principles.md."
doc_type: subsystem-index
status: active
last_reviewed: 2026-05-04
---

# Screening subsystem

`docs/screening/` は `records/03-candidates/` 生成と research handoff を支える subsystem 詳細です。今回の再編では subsystem として維持し、各 doc の責務を明示します。

`README.md` は目次と役割タグ表だけを扱います。設計原則本文の正本は [`principles.md`](./principles.md) です。

## Docs

| doc | role tag | 責務 |
| --- | --- | --- |
| [`principles.md`](./principles.md) | contract | screening subsystem の設計原則正本 |
| [`automation.md`](./automation.md) | runbook / automation | screening CLI の使い方と実装境界 |
| [`failure-taxonomy.md`](./failure-taxonomy.md) | reference | review / retro で使う失敗分類 |
| [`macro-gate-procedure.md`](./macro-gate-procedure.md) | runbook | outlook から research へ接続する Macro gate 手順 |
| [`mechanical.md`](./mechanical.md) | contract | 機械的ふるいの閾値と rule engine の意味論 |
| [`sector-region-map.md`](./sector-region-map.md) | reference | 東証 33 業種と outlook region の対応 |
| [`universe-rules.md`](./universe-rules.md) | contract | screening universe の境界条件 |
| [`valuation-metrics.md`](./valuation-metrics.md) | reference | valuation 指標の算出仕様 |

## 上位 docs

- 成果物 contract: [`../components/candidates.md`](../components/candidates.md)
- research handoff: [`../components/research.md`](../components/research.md)
- 操作入口: [`../operations/screening-runbook.md`](../operations/screening-runbook.md)
- automation map: [`../architecture/automation-map.md`](../architecture/automation-map.md)
