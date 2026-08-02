---
title: "Workflow — screening"
summary: "point-in-time cacheからcandidate、longlist、selectionを決定論的に生成し、人間レビューgateへ渡す工程。"
doc_type: workflow
status: active
last_reviewed: 2026-08-02
related_docs:
  - "../operations/decision-cycle.md"
  - "../reference/screening-runtime.md"
  - "../reference/valuation-metrics.md"
---

# Workflow — screening

screeningは全上場銘柄から割安ゾーンを機械抽出し、observed、derived、estimateを由来付きで出すL2工程である。採用・因果・相場観を判断せず、thesisの代わりにならない。

## Purpose and boundary

- `run`: point-in-timeの財務・価格・JPX factsからcandidate poolを作る。
- `select`: candidateを既存rulesでrankし、production recommendationsと、OP3レビューの入力母集団であるlonglistを出す。
- AI: longlistから[`decision-cycle` OP3 gate](../operations/decision-cycle.md#opportunity-human-review-gate-op3)のnarrative付きshortlistを作り、`baibai-app`の`/stocks/shortlist`レビュー面で人間へ提示する。
- human: reportからprimary-research setを選ぶ。
- research: primary-research setを一次情報、永久損失、3年/5年scenarioで比較し、最良0〜1件を決める。

macro context、ledger、予算はscreening rankを変更しない。後段のcontext/annotationとして扱う。

## Universe and ASOF

対象は東証Prime/Standard/Growthの普通株を基本とし、厳密なeligible universe、流動性、上場期間、業種相対ruleはversioned screening rulesを正本とする。ASOFは最新完全営業日。future dataとASOF後の開示を混ぜない。

historical backfill以外で`--allow-stale-jpx`を通常使用しない。ASOF、rules、SQLite coverage、run revision IDをoperation sessionへ残す。

## Cache coverage and refresh

正規順は次。

1. `verify-cache-coverage`
2. 不足時だけ`bootstrap-cache`
3. coverage状態にかかわらず`extract-edinet-metrics`
4. 初回coverageが不足していた場合だけ`verify-cache-coverage`を再実行
5. `run`（返された`run_revision_id`を保持）
6. `select --run-revision-id ...`（返された`selection_id`を保持）
7. review後に`shortlist publish`

coverage commandは単独で実行し、後続commandのexit 0で失敗を隠さない。`run/select`はprovider APIへ暗黙fallbackせず、cache-onlyで決定論的に動く。

この正規順（営業日判定 → coverage → 不足時のbootstrap → EDINET incremental extraction → 初回不足時のcoverage再検証 → run → select）とmacro series更新・read model export・run store pruneを東証営業日ごとに1コマンドで回す補助として`tools/cloud/daily_batch.py`がある。EDINETのdocument stateは日中にも変わり得るため、初回coverageがcompleteでもextractionを省略しない。変更のないmetric rowはbaselineから再利用する。export後の差分件数はbatch metricsへ載り、run通知にそのまま出る（通知は開かなくても読み手へ届く唯一の経路なので、その日の変化件数をそこへ置く）。screening後段の人間reviewは含まず`select`までの機械工程をorchestrateするscriptで、安定契約は各`baibai-engine` public CLI側に置く。使い方と失敗ポリシーは[`tools/cloud/README.md`](../../tools/cloud/README.md)を正本とする。

| coverage result | action |
| --- | --- |
| complete、ASOF一致 | EDINET incremental extractionを実行してrunへ進む |
| source range不足 | 該当sourceをbootstrapし、EDINET incremental extractionを実行して再検証 |
| future-dated row | stop。dataを修正する |
| stale JPX | historical backfill以外はstop |
| EDINET/price欠損 | 欠損範囲とcandidate影響を記録してstop/defer |

## Deterministic run

`run --asof`は同じcache、rules、ASOFから同じcandidate outputを作り、rebuildable run storeへtransactionalにpublishする。同一ASOFの再実行は別の`run_revision_id`を持つ。`--output-path`はDB publicationのYAML viewが必要な場合だけ指定し、raw全量をcanonical judgmentとしてcommitしない。

run storeは最新数世代を保持するcacheであり、容量に応じて`baibai-engine screening prune --keep N`で削除する。既定は3世代。run削除後もapplication DBのshortlist以降は各snapshotだけで読める。

candidateは次を区別する。

- **observed**: providerから観測したprice、financial、JPX fact。
- **derived**: formulaとinputを持つratio、percentile、relative metric。
- **estimate**: E[r]、FV anchor等のmodel output。事実ではない。

AI judgment、割安の原因、将来予測、採用結論をcandidateへ書かない。

## Select outputs

| output | 目的 | 読み手 | canonical |
| --- | --- | --- | --- |
| candidate pool | screen通過全件 | select/calibration | rebuildable |
| `recommendations` | production rule/cap適用後の通常表示 | operator | rebuildable |
| `longlist` | diversity/cap切断前のrank上位N件。OP3レビューの入力母集団 | AI/reviewer | rebuildable |
| shortlist | longlistからOP3の件数契約でselected暫定順位 / narrative（RR判断・catalyst・macro消化を含む）/ rejected理由を明示 | human review | application DB |
| primary-research set | shortlistのレビュー面から人間が選択 | research | workspace |

`--longlist-top 20`は候補抜けを点検するviewで、20件すべてを深掘りする命令ではない。`recommendations`のproduction capはshortlistの件数を決めない。review後はsource `selection_id`とrun/profile/context metadataを含むstrict draftを`baibai-engine screening shortlist publish`で明示publishする。machine recommendationをreview済みとして代用しない。

## Ranking versus warnings

| fact/context | rankを変える | warning/annotationだけ |
| --- | --- | --- |
| versioned screening ruleとestimate components | yes | — |
| liquidity/durability rule | rulesが定義する範囲 | 詳細reasonを出す |
| FV convergence | no | 現値が全ての利用可能なFV anchor以上、かつ`er_reversion_annual <= 0`なら`price_at_or_above_all_fv_anchors` |
| held/reserved | no | portfolio annotation |
| monthly budget/cash/concentration | no | proposal warning |
| macro material delta、macro contextの`as_of`の古さ | no | research context、context-level warning |
| corporate action unresolved | rankを都合よく変更しない | research/limitをblock |

上位候補をheld/reserved/予算だけで削除しない。一時的なFV乖離が大きく永久損失が低いなら買増し候補としてresearchへ残す。

FV convergence warning はselection longlistの調査入口だけに置く。入力はscreening candidateに保存済みの`market_price_yen`、`fv_sector_median_yen`、`fv_self_range_yen`、`er_reversion_annual`で、有限かつ正の価格・anchorと有限なreversionだけを利用する。anchorが2本なら現値が両方以上、1本ならその1本以上で、さらにreversionが0以下のとき`warning`とする。現値とanchorの等値は上値余地がないためwarning側に含める。anchorが0本、価格が無効、またはreversionが無効なら`not_evaluable`とし、欠損・未知・非数値を0へ補完しない。利用可能なanchorの名前と値、参考価格、reversionをprovenanceとしてpayloadへ残す。warningはscreen pass、自動除外、E[r]、rank、recommendationを変更しない。

## Corporate action and abnormal price

split/併合、株式交換、権利落ち、価格系列異常が疑われる場合はJPX/会社一次情報で効力日、株数、adjustment factor、price basisを確認する。adjusted seriesを指値へ使わない。unresolvedならresearch/plan-limitをdeferし、正常値を推測しない。

## No candidate and failure

- candidate 0件: screen条件とcoverageを確認し、正しければ正常終了。
- viable primary-research set 0件: `no actionable bargain`。購入を強制しない。
- estimate missing: observed factで穴埋めせず、原因と影響を示す。
- output drift: rules/input hashを確認し、古いworkspaceをpromoteしない。

## Calibration handoff

E[r]とFV anchorの長期予測力は`calibration-build/evaluate`でpoint-in-time panelとforward returnを比較する。3m/6mはregression alert、1yはleading evidence、production変更は3y/5y evidenceを必要とする。個別opportunityの完了条件へcalibrationを入れない。

## Failure / stop conditions

- coverage incomplete/future/stale。
- schema/rules/public CLIが不明。
- corporate actionとprice basisが矛盾。
- estimateをfactとして扱う、またはAI judgmentをcandidateへ書こうとしている。

## Validation

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-engine screening verify-cache-coverage --asof YYYY-MM-DD
UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_screening_run_store.py tests/test_screening_db_flow.py
```

## Related

- [`../operations/decision-cycle.md#opportunity-path`](../operations/decision-cycle.md#opportunity-path)
- [`./research.md`](./research.md)
- [`../reference/screening-runtime.md`](../reference/screening-runtime.md)
- [`../reference/valuation-metrics.md`](../reference/valuation-metrics.md)
