---
title: "Architecture index"
summary: "Entry point for the 3-layer infrastructure, the two loops, repository structure, and automation map."
doc_type: architecture
status: active
last_reviewed: 2026-06-21
---

# Architecture

`docs/architecture/` は Baibai-Loop の構造を扱います。読者が repo の正本構造を迷わず辿れることを優先します。正準の概念モデル（2 ループ・語彙）は [`../concepts.md`](../concepts.md) を参照します。

## Docs

| doc | 責務 |
| --- | --- |
| [`system-overview.md`](./system-overview.md) | 3 層インフラ（データ / 決定論的分析 / 判断）と、その上で動く 2 ループ（運用ループ / 改善ループ） |
| [`repository-map.md`](./repository-map.md) | root、`records/`、`records/_*`、`docs/`、`src/`、`tests`、`.github/` の責務 |
| [`automation-map.md`](./automation-map.md) | CLI、validator、position sync、schema、tests、CI の位置付け |
