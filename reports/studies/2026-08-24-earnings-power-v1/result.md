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

Shortlist v5、exact provenance、Review Set resolver、Research Gate bindingは採用済みValue / Carryだけで運用する。Earnings Power用Selection Policy YAMLとreplay evaluatorはnon-adoptionの証拠として保持するが、public CLI、selection payload、Shortlist schemaにはEarnings production pathを持たせない。
