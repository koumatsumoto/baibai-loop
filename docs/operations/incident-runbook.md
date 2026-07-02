---
title: "Incident runbook"
summary: "データ取得失敗・validation 失敗・自動処理の失敗に対応するときの入口。"
doc_type: operation
status: active
last_reviewed: 2026-06-23
---

# Incident runbook

運用中に records の作成や自動処理が止まったときの入口。投資判断の代わりではなく、事実の生成と検証の再現性を守るために扱う。

## Source access failure

- macro context の Tier 1 取得失敗は [`../reference/data-sources.md`](../reference/data-sources.md) の運用に従う。
- 値を別 source で埋める場合は、Tier と `status` の扱いを明示する。
- 取得失敗を `未公表` と混同しない。

## Validation failure

1. `uv run baibai-loop-validation` の error path を読む。
2. 対応する component doc と schema を確認する。
3. schema や validator の意味を推測で変えない。必要なら別 issue を起こす。

## Automation failure

- screening CLI failure は [`../reference/screening-runtime.md`](../reference/screening-runtime.md) と [`../architecture.md#automation`](../architecture.md#automation) を確認する。
- position 記録の validation failure は [`../workflow/position.md`](../workflow/position.md) を確認する。
- CI / local parity は [`../reference/python-foundation.md`](../reference/python-foundation.md) を確認する。
