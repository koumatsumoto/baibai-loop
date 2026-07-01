---
title: "Reference index"
summary: "Stable reference for valuation metrics, screening runtime, data sources, configuration, validation, and Python foundation."
doc_type: reference-index
status: active
last_reviewed: 2026-05-04
---

# Reference

`docs/reference/` は日々の runbook から参照される、変更頻度の低い基盤情報を置きます。

| doc | 責務 |
| --- | --- |
| [`valuation-metrics.md`](./valuation-metrics.md) | valuation 指標の算出仕様 |
| [`screening-runtime.md`](./screening-runtime.md) | screening CLI / provider / EDINET・JPX / SQLite schema の実装仕様 |
| [`data-sources.md`](./data-sources.md) | data source tier、取得失敗時の扱い、取得データ cache の保存方針 |
| [`configuration.md`](./configuration.md) | runtime config、credentials、environment variable の入口 |
| [`testing-and-validation.md`](./testing-and-validation.md) | `records/_schemas/`、validator、test / CI verification |
| [`python-foundation.md`](./python-foundation.md) | Python runtime、dependency、quality gate、CI parity |
| [`jquants-rate-limits.md`](./jquants-rate-limits.md) | J-Quants レート制限と bootstrap コストの観測メモ |
