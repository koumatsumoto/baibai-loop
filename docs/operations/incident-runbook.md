---
title: "Incident runbook"
summary: "Operational entry point for source access failures, validation failures, and automation failures."
doc_type: operation
status: active
last_reviewed: 2026-05-04
---

# Incident runbook

Incident は運用中に records 作成や automation が止まった場合の入口です。投資判断の代替ではなく、事実生成と検証の再現性を守るために扱います。

## Source access failure

- macro context の Tier 1 取得失敗は [`../reference/data-sources.md`](../reference/data-sources.md) の運用に従う。
- 値を別 source で埋める場合は、Tier と `status` の扱いを明示する。
- 取得失敗を `未公表` と混同しない。

## Validation failure

1. `uv run baibai-loop-validation` の error path を読む。
2. 対応する component doc と schema を確認する。
3. schema や validator の意味を推測で変えない。必要なら別 issue を起こす。

## Automation failure

- screening CLI failure は [`../screening/automation.md`](../screening/automation.md) と [`../architecture/automation-map.md`](../architecture/automation-map.md) を確認する。
- ledger sync failure は [`../components/ledger.md`](../components/ledger.md) を確認する。
- CI / local parity は [`../reference/python-foundation.md`](../reference/python-foundation.md) を確認する。
