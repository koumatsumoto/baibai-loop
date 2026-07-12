---
title: "Reference index"
summary: "artifact、式、data source、runtime、validationの安定contractを調べる入口。"
doc_type: reference-index
status: active
last_reviewed: 2026-07-12
---

# Reference

referenceは「artifact・式・error/warningは何を意味するか」を持つ。e2e順序は[`operations`](../operations/)、工程内の変換は[`workflow`](../workflow/)、field/type/enumはJSON schema、CLI optionはpublic `--help`を正本とする。

| 調べたいこと | reference |
| --- | --- |
| decision packet、3年/5年算術、review hash、planning limit | [`decision-packet.md`](./decision-packet.md) |
| cash、reservation、execution、release、snapshot | [`portfolio-ledger.md`](./portfolio-ledger.md) |
| hold/add/reduce/exitと税引後代替 | [`holding-review.md`](./holding-review.md) |
| portfolio returnとTOPIX観測 | [`portfolio-ledger.md#historical-outcome`](./portfolio-ledger.md#historical-outcome) |
| long-horizon estimate calibration | [`estimate-calibration.md`](./estimate-calibration.md) |
| valuation指標 | [`valuation-metrics.md`](./valuation-metrics.md) |
| screening CLI、SQLite、provider | [`screening-runtime.md`](./screening-runtime.md) |
| source tierと取得失敗 | [`data-sources.md`](./data-sources.md) |
| schema、validator、docs/skill drift | [`testing-and-validation.md`](./testing-and-validation.md) |
| Python、dependency、quality gate、CI | [`python-foundation.md`](./python-foundation.md) |

referenceは運用sessionの進捗やIssue固有の作業履歴を持たない。
