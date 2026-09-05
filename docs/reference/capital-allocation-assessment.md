---
title: "Capital Allocation Assessment"
summary: "深掘りした候補の横比較、buyまたは見送り、content review束縛を1つのimmutable判断へ固定する契約。"
doc_type: reference
status: active
---

# Capital Allocation Assessment

Capital Allocation Assessment は、1 research cycle で深掘りした候補を比較し、`allocate / no_allocation / defer` の最終結論を固定する canonical judgment である。候補ごとの根拠は promoted Thesis と Thesis Review、cycle 全体の比較と見送り理由はこの判断が所有する。

## 境界

- `allocate` は selected alternative をちょうど1件持ち、その Thesis ID、recorded core hash、Thesis Review IDへ束縛する。
- `no_allocation` と `defer` は selected alternativeを持たない。
- 指値、数量、notional、expiryはassessmentへ保存しない。`research plan-limit --capital-allocation-assessment-id <ASSESSMENT_ID>`が、公開済み`allocate`判断に束縛されたThesis / Review、current ledger、前営業日raw closeから都度計算する。
- broker操作は人間だけが行う。broker factはassessment IDを`decision_reference`としてledger draftへ変換する。

判断の散文はCapital Allocation Assessmentが正本だが、5年base CAGR、要求リターン、FV、FV乖離、break-even、永久損失結論は正本ではない。read surfaceはbound immutable thesisから再導出し、payloadへ複写しない。

## Schema v1

top-levelは`schema_version / kind / capital_allocation_assessment_id / as_of / published_at / result / headline / research_triage_id / macro_context_id / comparison / forgone / alternatives / review`を持つ。alternativeはticker、disposition、具体的理由、thesis/review bindingだけを持つ。Thesis由来のmachine scalarは複写しない。

persisted field `result / review / forgone`はstorage contractとして維持する。`review`はassessment draft digestへ束縛するcontent reviewであり、個別ThesisへのThesis Reviewとは別である。

旧schemaをruntimeでprojectせず、未知versionは明示errorにする。

## Publish gate

publishは少なくとも次を拒否する。

1. activeな`capital-allocation` Operationのexact Research Triage・人間確定Research Setと一致しない比較対象（欠落・混入を含む）。scaffoldとcheckも同じ集合を検証し、開始前のAssessmentを別cycleへ流用しない
2. source Research Triageで`research`とされていないticker
3. thesis ID、ticker、recorded core hashの不一致
4. thesisに束縛されないreview、または`allocate` alternativeのreview欠損
5. allocate gate未達、永久損失結論elevated、必要なhuman evidence override欠損
6. content review済みdraft digestとの不一致

## 手順

```bash
uv run baibai-engine research capital-allocation-scaffold \
  --db stores/application/baibai.sqlite \
  --capital-allocation-assessment-id <capital_allocation_assessment_id> \
  --asof YYYY-MM-DD \
  --research-triage-id <research_triage_id> \
  --thesis-id <thesis_id> --out <draft>

uv run baibai-engine research capital-allocation-publish \
  <draft> --db stores/application/baibai.sqlite --check

uv run baibai-engine research capital-allocation-publish \
  <reviewed-draft> --db stores/application/baibai.sqlite
```

`--thesis-id`は調査したcaseごとに反復する。`--check`で示されるdigestへcontent reviewを束縛し、修正後はcontent reviewをやり直す。non-promoted research artifactはoperation sessionに必要最小限をsnapshotし、canonical thesis/review/assessment payloadを複製しない。
