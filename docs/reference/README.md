---
title: "Reference index"
summary: "valuation 指標・screening 実装・データソース・設定・検証・Python 基盤の、変更頻度の低い参照情報の入口。"
doc_type: reference-index
status: active
last_reviewed: 2026-05-04
---

# Reference

`docs/reference/` には、日々の手順から参照される変更頻度の低い基盤情報を置く。

| doc | 責務 |
| --- | --- |
| [`valuation-metrics.md`](./valuation-metrics.md) | valuation 指標の算出仕様 |
| [`estimate-calibration.md`](./estimate-calibration.md) | 長期見積り較正リプレイ（PIT panel / forward return / 評価指標）の実装仕様 |
| [`screening-runtime.md`](./screening-runtime.md) | screening CLI / provider / EDINET・JPX / SQLite schema の実装仕様 |
| [`data-sources.md`](./data-sources.md) | data source tier、取得失敗時の扱い、取得データ cache の保存方針 |
| [`configuration.md`](./configuration.md) | runtime config、credentials、environment variable の入口 |
| [`testing-and-validation.md`](./testing-and-validation.md) | `records/_schemas/`、validator、test / CI verification |
| [`python-foundation.md`](./python-foundation.md) | Python runtime、dependency、quality gate、CI parity |
| [`jquants-rate-limits.md`](./jquants-rate-limits.md) | J-Quants レート制限と bootstrap コストの観測メモ |
