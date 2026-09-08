---
title: "Capital Allocation Assessment"
summary: "深掘りした候補の横比較、buyまたは見送り、content review束縛を1つのimmutable判断へ固定する契約。"
doc_type: reference
status: active
---

# Capital Allocation Assessment

Capital Allocation Assessment は、人間のexact Research Setで深掘りした候補を比較し、`allocate / no_allocation / defer` の最終結論を固定する canonical judgment である。候補ごとの根拠は promoted Thesis と Thesis Review、cycle 全体の比較と見送り理由はこの判断が所有する。

## 境界

- `allocate` は selected alternative をちょうど1件持ち、その Thesis ID、recorded core hash、Thesis Review IDへ束縛する。
- `no_allocation` と `defer` は selected alternativeを持たない。
- 指値、数量、notional、expiryはassessmentへ保存しない。`research plan-limit --capital-allocation-assessment-id <ASSESSMENT_ID>`が、公開済み`allocate`判断に束縛されたThesis / Review、確認済み資本、利用可能な最新確定raw closeから都度計算する。正式判断日は当日であり、発注sessionとは別である。`--target-session`は既存calendarの次の有効営業日（引け前は当日）を指定し、`planned_limit`だけが将来の15:30 JST期限を返す。calendar不明・失効sessionはdeferし、期限を偽装しない。
- broker操作は人間だけが行う。broker factはassessment IDを`decision_reference`としてledger draftへ変換する。

判断の散文はCapital Allocation Assessmentが正本だが、原評価のBase/Downside、要求リターン、Pmax、企業評価の成立性は正本ではない。read surfaceはbound immutable thesisから再導出し、payloadへ複写しない。

## Schema v1

top-levelは`schema_version / kind / capital_allocation_assessment_id / as_of / published_at / result / headline / research_triage_id / macro_context_id / comparison / forgone / alternatives / review`を持つ。alternativeはticker、disposition、具体的理由、thesis/review bindingだけを持つ。Thesis由来のmachine scalarは複写しない。

persisted field `result / review / forgone`はstorage contractとして維持する。`review`はassessment draft digestへ束縛するcontent reviewであり、個別ThesisへのThesis Reviewとは別である。

旧schemaをruntimeでprojectせず、未知versionは明示errorにする。

## Publish gate

checkとpublishは共通のReviewed Thesis読込で全候補のexact pairを確認する。allocate対象だけに現在の価格・評価日・policy floor・保有・予約・同CAA約定履歴・cashを適用する。比較対象にはreject・unresolved・既保有を含められる。新規publicationの`published_at`はwriterが確定する。
同じID・内容・content reviewの再送は、Operation完了後でも保存済みpublicationを返す。
時刻以外の内容が異なる再送は拒否し、既存rowは変更しない。

publishは少なくとも次を拒否する。

1. activeな`capital-allocation` Operationのexact Research Triage・人間確定Research Setと一致しない比較対象（欠落・混入を含む）。scaffoldとcheckも同じ集合を検証し、開始前のAssessmentを別cycleへ流用しない
2. source Research Triageで`research`とされていないticker
3. thesis ID、ticker、recorded core hashの不一致
4. thesisに束縛されないreview、または`allocate` alternativeのreview欠損
5. allocate対象の新規適格性未達。candidateかつintact/resolvedでもPmax合格だけでは配分しない
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

同じResearch SetからCAA-1→人間報告→CAA-2を順次公開できる。企業調査を繰り返さず最新cashを用い、Operationは最後のCAAを参照して完了する。Thesis Reviewは企業別検算、CAA content reviewは比較・配分だけを所有する。

Planningの`portfolio_exposure.common_factor_unclassified_tickers`は、保有・予約と今回の配分候補の未分類tickerを示す。非空なら`portfolio_exposure_common_factor_coverage_incomplete`を表示し、既知factorの比率は下限として読む。自動分類や停止条件にはせず、既知factorの集中warningも維持する。
