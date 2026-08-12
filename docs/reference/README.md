---
title: "Reference index"
summary: "artifact、式、data source、runtime、validationの安定contractを調べる入口。"
doc_type: reference-index
status: active
---

# Reference

referenceは「artifact・式・error/warningは何を意味するか」を持つ。運用手順は[`.agents/skills/`](../../.agents/skills/)の各SKILL.md、field/type/enumはDB constraintとengine model、CLI optionはpublic `--help`を正本とする。

| 調べたいこと | reference |
| --- | --- |
| thesis、3年/5年算術、review hash、planning limit | [`thesis.md`](./thesis.md) |
| lane横比較、購入方法、content review束縛、統合判断の正本 | [`bargain-assessment.md`](./bargain-assessment.md) |
| business model別の問いとclaim triangulation | [`business-model-research.md`](./business-model-research.md) |
| cash、reservation、execution、release、snapshot | [`portfolio-ledger.md`](./portfolio-ledger.md) |
| hold/add/reduce/exitと税引後代替 | [`holding-review.md`](./holding-review.md) |
| portfolio returnとTOPIX観測 | [`portfolio-ledger.md#historical-outcome`](./portfolio-ledger.md#historical-outcome) |
| long-horizon estimate calibration | [`estimate-calibration.md`](./estimate-calibration.md) |
| valuation指標 | [`valuation-metrics.md`](./valuation-metrics.md) |
| screening CLI、SQLite、provider、selectの判断境界 | [`screening-runtime.md`](./screening-runtime.md) |
| macro layer（L1/L2/L3・深度契約・8レンズ・scorecard） | [`macro.md`](./macro.md) |
| source tierと取得失敗 | [`data-sources.md`](./data-sources.md) |
| lakeのbuild/publish/固定release読み/projection/L2 retention/parity | [`market-lake.md`](./market-lake.md) |
| write-time validation 層 | [`../architecture.md`](../architecture.md) Development gates |
| Python、dependency、quality gate、CI | [`python-foundation.md`](./python-foundation.md) |

referenceは運用sessionの進捗やIssue固有の作業履歴を持たない。
