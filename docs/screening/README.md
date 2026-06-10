---
title: "Screening subsystem"
summary: "Index and role map for the screening subsystem docs. Principles remain canonical in principles.md."
doc_type: subsystem-index
status: active
last_reviewed: 2026-05-04
---

# Screening subsystem

`docs/screening/` は `records/04-candidates/` 生成と research handoff を支える subsystem 詳細です。各 doc は screening の責務境界を明示します。

`README.md` は目次と役割タグ表だけを扱います。設計原則本文の正本は [`principles.md`](./principles.md) です。

## Docs

| doc | role tag | 責務 |
| --- | --- | --- |
| [`principles.md`](./principles.md) | contract | screening subsystem の設計原則正本 |
| [`automation.md`](./automation.md) | runbook / automation | screening CLI の使い方と実装境界 |
| [`failure-taxonomy.md`](./failure-taxonomy.md) | reference | review / retro で使う失敗分類 |
| [`lane-cohorts-2026-05.md`](./lane-cohorts-2026-05.md) | reference | lane 別 cohort forward-return telemetry の初回スコアボード (2026-05) |
| [`mechanical.md`](./mechanical.md) | contract | 機械的ふるいの閾値と rule engine の意味論 |
| [`regime-lens-replay-2026-05.md`](./regime-lens-replay-2026-05.md) | reference | market regime lens の on/off replay 検証 (2026-05) |
| [`selection-ablation-2026-05.md`](./selection-ablation-2026-05.md) | reference | selection 構成要素・lane 別 ablation の効果計測 (2026-05) |
| [`sector-sensitivity-map.md`](./sector-sensitivity-map.md) | reference | 東証 33 業種と macro context sector tilt の参考分類 |
| [`universe-rules.md`](./universe-rules.md) | contract | screening universe の境界条件 |
| [`valuation-metrics.md`](./valuation-metrics.md) | reference | valuation 指標の算出仕様 |

## 上位 docs

- 成果物 contract: [`../components/candidates.md`](../components/candidates.md)
- research handoff: [`../components/research.md`](../components/research.md)
- 操作入口: [`../operations/screening-runbook.md`](../operations/screening-runbook.md)
- automation map: [`../architecture/automation-map.md`](../architecture/automation-map.md)
