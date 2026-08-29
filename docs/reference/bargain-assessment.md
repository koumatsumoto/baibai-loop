---
title: "Bargain assessment"
summary: "深掘りした候補の横比較、buyまたは見送り、content review束縛を1つのimmutable判断へ固定する契約。"
doc_type: reference
status: active
---

# Bargain Assessment

Bargain Assessment は、1 opportunity cycle で深掘りした候補を比較し、`buy / no_actionable_bargain / defer` の最終結論を固定する canonical judgment である。候補ごとの根拠は promoted thesis と independent review、cycle 全体の比較と見送り理由は assessment が所有する。

## 境界

- `buy` は selected case をちょうど1件持ち、その thesis ID、recorded core hash、independent review IDへ束縛する。
- `no_actionable_bargain` と `defer` は selected caseを持たない。
- 指値、数量、notional、expiryはassessmentへ保存しない。必要時に `research plan-limit` がcurrent ledgerと前営業日raw closeから計算するephemeral outputである。
- broker操作は人間だけが行う。human-confirmed order resultはassessment IDを`decision_reference`としてledger draftへ変換する。

判断の散文はassessmentが正本だが、5年base CAGR、要求リターン、FV、FV乖離、break-even、永久損失結論は正本ではない。publishはpromoted thesisから再導出し、draftのmachine値と照合する。

## Schema v4

top-levelは`schema_version / kind / assessment_id / as_of / published_at / result / headline / shortlist_id / macro_context_id / comparison / forgone / cases / review`を持つ。caseはticker、disposition、理由、thesis/review binding、machine値、事業・価値獲得・成長品質・財務耐性・countercase・catalyst・research question・unknown・source caveatを持つ。

旧schemaをruntimeでprojectしない。one-time application DB cutoverはv3のcurrent assessmentをv4へ変換し、監査だけに使われたv1/v2 historyはcurrent storeへ移さない。未知versionは明示errorにする。

## Publish gate

publishは少なくとも次を拒否する。

1. source shortlistに含まれないticker
2. thesis ID、ticker、recorded core hashの不一致
3. thesisに束縛されないreview、またはbuy caseのreview欠損
4. thesisから再導出したmachine値との差
5. buy gate未達、永久損失結論elevated、必要なhuman evidence override欠損
6. review済みdraft digestとの不一致

## 手順

```bash
uv run baibai-engine research assessment-scaffold \
  --db stores/application/baibai.sqlite \
  --assessment-id <assessment_id> --asof YYYY-MM-DD \
  --shortlist-id <shortlist_id> \
  --thesis-id <thesis_id> --out <draft>

uv run baibai-engine research assessment-publish \
  --db stores/application/baibai.sqlite --draft <draft> --check

uv run baibai-engine research assessment-publish \
  --db stores/application/baibai.sqlite --draft <reviewed-draft>
```

`--thesis-id`は調査したcaseごとに反復する。`--check`で示されるdigestへ独立reviewを束縛し、修正後は再reviewする。non-promoted research artifactはoperation sessionに必要最小限をsnapshotし、canonical thesis/review/assessment payloadを複製しない。
