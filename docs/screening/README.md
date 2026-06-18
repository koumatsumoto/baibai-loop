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
| [`extending.md`](./extending.md) | reference | 新 lens / lane / telemetry の拡張点と計測ファースト手順 |
| [`mechanical.md`](./mechanical.md) | contract | 機械的ふるいの閾値と rule engine の意味論 |
| [`universe-rules.md`](./universe-rules.md) | contract | screening universe の境界条件 |
| [`valuation-metrics.md`](./valuation-metrics.md) | reference | valuation 指標の算出仕様 |

dated 計測結果（過去の replay / ablation / regime-lens / lane-cohorts）は [`../../docs/operations/backtest-runbook.md`](../operations/backtest-runbook.md) §6 と [`../../reports/`](../../reports/) に統合済み。新しい計測は backtest-runbook の 7 axis に従い `.cache/` で実施する。

## 上位 docs

- 成果物 contract: [`../components/candidates.md`](../components/candidates.md)
- research handoff: [`../components/research.md`](../components/research.md)
- 操作入口: [`../operations/screening-runbook.md`](../operations/screening-runbook.md)
- system 全体: [`../architecture/system-overview.md`](../architecture/system-overview.md)
