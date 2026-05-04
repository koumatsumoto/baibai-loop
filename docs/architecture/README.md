---
title: "Architecture index"
summary: "Entry point for current structure, repository map, information flow, automation map, and architecture decisions."
doc_type: architecture
status: active
last_reviewed: 2026-05-04
---

# Architecture

`docs/architecture/` は Baibai-Loop の現在の構造を扱います。過去の移行理由は ADR に分け、ここでは読者が repo の正本構造を迷わず辿れることを優先します。

## Docs

| doc | 責務 |
| --- | --- |
| [`system-overview.md`](./system-overview.md) | 4 成分 + 下流 2 成分、macro / micro track、スコープと非目標 |
| [`repository-map.md`](./repository-map.md) | root、`records/`、`records/_*`、`docs/`、`src/`、`tests`、`.github/` の責務 |
| [`information-flow.md`](./information-flow.md) | brief -> outlook -> candidates -> research -> trades -> reviews と feedback loop |
| [`automation-map.md`](./automation-map.md) | CLI、validator、ledger sync、schema、tests、CI の位置付け |
| [`decisions/`](./decisions/) | 設計判断の ADR seed |

## 旧 path との関係

[`../architecture.md`](../architecture.md) は互換 shim として残しています。既存 issue、PR、component docs からの参照を守るため旧本文を保持していますが、新規 docs からはこの directory の docs を参照します。
