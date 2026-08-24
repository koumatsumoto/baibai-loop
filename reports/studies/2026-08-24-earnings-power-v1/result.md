---
title: "Earnings Power v1 frozen replay result"
date: 2026-08-24
status: inconclusive_non_adoption
issues: [1041, 1064]
---

価値tier: T1 — Value / Carry以外のResearch Gate供給を増やす案を、長期outcomeと未解決感応度で反証した。

# 結論

固定済みA′契約によるhistorical replayのverdictは`inconclusive`。Earnings Power Lane、bounded Attention、Shadow、Pilotはproductionへ採用しない。`normalized_per_3fy`はDerived Metricのraw annotationとして残し、正常利益、FV、事実とは呼ばない。

機械結果の正本は[`historical-replay.yaml`](./historical-replay.yaml)、事前固定した契約は[`preregistration.md`](./preregistration.md)である。threshold、minimum condition、ordering、diagnostic、block、K、windowは結果を見て変更していない。

# 固定verdictを決めた観測

- canonical eligible cohortは3yが35、5yが19で、各horizonの最低12を満たした。
- cohort-equal medianのas-reported差（Earnings Power − Value / Carry）は3y price `+0.2954`、3y total `+0.2844`、5y price `+0.4264`、5y total `+0.0120`で、4組とも非負だった。
- 5y total-return coverageはEarnings Power `73.16%`で、固定floor `75%`を下回った。
- 5y total-return差はas-reported `+0.0120`に対し、neutral / failure感応度でともに`-0.1454`となり、符号が割れた。
- alt-only件数のcohort medianは18、sector concentrationのcohort medianは25%だった。供給geometryは満たしたが、outcome uncertaintyを上書きしない。

# 実装上の帰結

Shortlist v5、exact provenance、Review Set resolver、Research Gate bindingは採用済みValue / Carryだけで運用する。固定Selection Policy入力はstudy artifactとして保持する。active `method/` policy、replay evaluator、public CLI、selection payload、Shortlist schemaにはEarnings production pathを持たせない。

# 2026-08-24 完成監査で確認した limitation

verdict、threshold、window、K、diagnostic は変更していない。以下は結論を覆す観測ではなく、**次の Lane study が同じ曖昧さを持たないための記録**である。要求する最小 artifact は [`estimate-calibration.md`](../../../docs/reference/estimate-calibration.md#別-opportunity-lane-を検証する-study-の最小-artifact) を正本とする。

- [`historical-replay.yaml`](./historical-replay.yaml) は cohort ごとの件数・overlap・sector concentration を持つが、ticker membership を持たない。どの銘柄が `alt_only` として差を作ったかは、この artifact だけからは再構成できない。
- [`frozen-policy.yaml`](./frozen-policy.yaml) は Policy Diagnostic として `earnings-power-leverage-risk-v1` と `earnings-power-historical-special-gain-risk-v1` を固定したが、replay dataset がその判定に必要な Fact を保持していないため、artifact には値も `not_observable` も残っていない。diagnostic が実際に何件へ立ったかは事後に確認できない。
- calibration panel は `jpx_flags_by_ticker={}` で universe を再生する（`engine/src/baibai_engine/screening/calibration/panel.py`）。production の共通 gate である `required_jpx_flags`（特別注意銘柄 / 整理銘柄 / 取引停止、`exclude_jpx_flagged: true`）による除外は、両 Lane とも歴史的に適用されていない。これは「該当 0 件だった」ではなく **point-in-time 再現不能**である。
