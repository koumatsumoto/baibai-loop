---
title: "Architecture index"
summary: "Entry point for repository structure, information flow, automation map, and architecture decisions."
doc_type: architecture
status: active
last_reviewed: 2026-05-04
---

# Architecture

`docs/architecture/` は Baibai-Loop の構造を扱います。読者が repo の正本構造を迷わず辿れることを優先します。

## Docs

| doc | 責務 |
| --- | --- |
| [`system-overview.md`](./system-overview.md) | portfolio policy -> observations -> regime view -> screen output -> investment memo -> execution -> attribution -> playbook feedback |
| [`repository-map.md`](./repository-map.md) | root、`records/`、`records/_*`、`docs/`、`src/`、`tests`、`.github/` の責務 |
| [`information-flow.md`](./information-flow.md) | brief -> outlook -> candidates -> research -> trades -> reviews と feedback loop |
| [`automation-map.md`](./automation-map.md) | CLI、validator、ledger sync、schema、tests、CI の位置付け |
| [`decisions/`](./decisions/) | 設計判断の ADR seed |

新規 docs は `docs/architecture/` 配下を参照します。
