---
title: "Capital Allocation Assessment"
summary: "深掘りした候補の横比較、buyまたは見送り、content review束縛を1つのimmutable判断へ固定する契約。"
doc_type: reference
status: active
---

# Capital Allocation Assessment

Capital Allocation Assessment は、1 research cycle で深掘りした候補を比較し、`allocate / no_allocation / defer` の最終結論を固定する canonical judgment である。候補ごとの根拠は promoted thesis と independent review、cycle 全体の比較と見送り理由はこの判断が所有する。

## 境界

- `allocate` は selected alternative をちょうど1件持ち、その thesis ID、recorded core hash、independent review IDへ束縛する。
- `no_allocation` と `defer` は selected alternativeを持たない。
- 指値、数量、notional、expiryはassessmentへ保存しない。必要時に `research plan-limit` がcurrent ledgerと前営業日raw closeから計算するephemeral outputである。
- broker操作は人間だけが行う。human-confirmed order resultはassessment IDを`decision_reference`としてledger draftへ変換する。

判断の散文はCapital Allocation Assessmentが正本だが、5年base CAGR、要求リターン、FV、FV乖離、break-even、永久損失結論は正本ではない。read surfaceはbound immutable thesisから再導出し、payloadへ複写しない。

## Schema v1

top-levelは`schema_version / kind / capital_allocation_assessment_id / as_of / published_at / result / headline / research_triage_id / macro_context_id / comparison / forgone / alternatives / review`を持つ。alternativeはticker、disposition、具体的理由、thesis/review bindingだけを持つ。Thesis由来のmachine scalarは複写しない。

旧schemaをruntimeでprojectせず、未知versionは明示errorにする。

## Publish gate

publishは少なくとも次を拒否する。

1. source Research Triageで`research`とされていないticker
2. thesis ID、ticker、recorded core hashの不一致
3. thesisに束縛されないreview、または`allocate` alternativeのreview欠損
4. allocate gate未達、永久損失結論elevated、必要なhuman evidence override欠損
5. review済みdraft digestとの不一致

## 手順

```bash
uv run baibai-engine research capital-allocation-scaffold \
  --db stores/application/baibai.sqlite \
  --assessment-id <capital_allocation_assessment_id> --asof YYYY-MM-DD \
  --research_triage-id <research_triage_id> \
  --thesis-id <thesis_id> --out <draft>

uv run baibai-engine research capital-allocation-publish \
  --db stores/application/baibai.sqlite --draft <draft> --check

uv run baibai-engine research capital-allocation-publish \
  --db stores/application/baibai.sqlite --draft <reviewed-draft>
```

`--thesis-id`は調査したcaseごとに反復する。`--check`で示されるdigestへ独立reviewを束縛し、修正後は再reviewする。non-promoted research artifactはoperation sessionに必要最小限をsnapshotし、canonical thesis/review/assessment payloadを複製しない。
