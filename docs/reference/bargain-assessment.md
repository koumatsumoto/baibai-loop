---
title: "Bargain assessment"
summary: "深掘りしたlaneの横比較、購入方法または見送り理由、content review束縛を1つのimmutable判断文書へ固定する契約。"
doc_type: reference
status: active
last_reviewed: 2026-07-29
---

# bargain assessment — 割安機会評価

opportunity pathでprimary-research setを調べ終えた後の統合判断。「今どれが最もお買い得か」と「どう買うか / なぜ買わないか」を1つのimmutable revisionへ固定する。shortlistが「どれを調べるか」の判断であるのに対し、これは「調べ終えて何を結論したか」の判断であり、購入提案を作らないサイクル（`no_actionable_bargain` / `defer`）にも成立する。

application DBの`bargain_assessment`が正本で、Baibai LoopのStocks面がindex → 詳細で読む口になる。

## Artifact boundary

| artifact | 責務 | canonical / ephemeral |
| --- | --- | --- |
| lane `thesis-draft.yaml` / promoted thesis | sources、facts、3年/5年scenario、FV、7軸、AI value capture、countercase | promoted thesisだけcanonical |
| `research-comparison.yaml` | 全laneの比較、disposition、非採用理由、最良0〜1件 | workspace内ephemeral judgment |
| `proposal` row | thesis/review/ledgerに束縛したraw close、max price、指値、数量、notional、expiry、warning | canonical |
| `bargain-assessment.yaml` draft | 記入前の骨格と、機械導出済みの数値 | ephemeral draft |
| `bargain_assessment` row | 統合判断、lane digest、購入方法、review束縛 | **canonical** |
| operation session artifact | non-promoted thesisと調査全文のsnapshot | persistent session payload |

判断の散文はassessmentが正本だが、**数値は正本ではない**。5年base CAGR、要求リターン、FV、FV乖離、break-even、永久損失結論、指値、数量、想定約定額はpromoted thesisとproposalから機械で導出する。publishは同じ導出をやり直してdraftの値と照合するので、scaffold後に手で書き換えた数値は保存されない。

調査の全文はthesisとoperation session artifactに残り、assessmentには判断に必要な要点だけを置く。

## Lane digest の責務

各laneは深掘りの結論を次の要点へ圧縮する。thesisの複製ではなく、**laneを採否した理由が読み取れる最小限**にする。

- `disposition` / `disposition_reason` — `selected` / `reject` / `defer` と、そう決めた理由
- `reject_class` — `reject` / `defer`の主因を集計する分類。自由記述を置き換えず、自動除外やrankingには使わない
- `business_model` — 何で稼いでいるか
- `value_capture` — 売上が利益・FCF・一株価値へ届く経路
- `growth_quality` — volume / price / mix / upsell・churn / FXを開示範囲で分けた成長の質
- `financial_resilience` — 悪いシナリオで生き残る根拠
- `strongest_countercase` — 最も強い反対仮説
- `catalyst` — 再評価のdated catalyst
- `research_questions` — shortlist narrativeの`research`確認事項の決着。`answered`なら回答、`unresolved`なら確認できなかった理由を書く
- `unknowns` — 未解決の不確実性
- `source_caveats` — `missing / stale / failed / blocked` sourceと、その欠落が判断に与える影響

`research_questions`は1件以上必須とする。候補がresearch slotを得た理由そのものなので、答えられなかった場合も`unresolved`として残し、黙って落とさない。scaffoldはshortlist narrativeの`research`をquestion欄へ先に置く。

`missing / failed / blocked` sourceを観測事実の根拠に使わない。使えなかったなら`source_caveats`と`unknowns`へ出す。

## 機械値の束縛

`lanes[].machine`はpromoted thesisからの導出値で、scaffoldが書きpublishが照合する。

| field | 導出元 |
| --- | --- |
| `five_year_base_cagr_pct` | thesis評価の5年base scenario total return CAGR |
| `required_return_pct` | `estimates.required_5y_base_cagr_pct` |
| `fair_value_yen` / `fv_gap_pct` | `estimates.current_fair_value_yen`と`entry_price_basis_yen` |
| `base_terminal_multiple` / `break_even_terminal_multiple` / `terminal_multiple_buffer` | 5年base break-even |
| `break_even_earnings_growth_pct` / `earnings_growth_buffer_pp` | 同上 |
| `observed_trailing_multiple` | 同上 |
| `permanent_loss_conclusion` / `adverse_risk_axes` | 7軸permanent-loss riskからの結論と、`adverse`な軸 |

リターン側だけでなく永久損失の結論も機械経路で供給する。リスクリワードは片側だけでは読めない。bufferが負であることや`elevated`は表示上の欠陥ではなく、買い提案との整合はreview gateとlaneの`disposition_reason`の責務とする。

`purchase`はproposal rowからの導出値で、payload hashと`planned_limit`の指値・数量・notional・上限価格・終値・expiryを照合する。proposalはengineの`plan-limit`が書いたcanonical rowなので、exposure比率やwarning閾値をassessment側で再計算しない。cap抵触は`warnings`として運ばれ、レポートへ表示する。

## Publish の fail-closed 条件

`assessment-publish`は次のいずれかで停止する。

1. laneのtickerがbound shortlistの`selected`集合に無い
2. lane `thesis_id`のtickerがlaneのtickerと違う
3. `thesis_core_sha256`がstore上のthesisと違う。draft作成後にthesisが動いた場合
4. `machine`の値がthesisからの再導出と違う。表示桁の丸め以外の書き換え
5. `purchase.proposal_id`が存在しない、tickerが違う、選択laneのthesisを束縛していない
6. `proposal_sha256`がstore上のproposalと違う、または`planned_limit`の数値・expiryが違う
7. `as_of`がbound shortlistの`as_of`より前
8. `review.draft_sha256`がdraft内容のhashと違う
9. `reject` / `defer` laneの`reject_class`が欠ける、未定義である、または`selected` laneに付いている
10. 同じ`assessment_id`が別内容で既にpublishされている

`result`と`purchase`の整合はschemaが持つ。`proposal`はselected lane 1件と`purchase`と`entry_timing`を必須とし、`no_actionable_bargain` / `defer`はselected laneも`purchase`も持てない。

## 独立 content review

`review.draft_sha256`は**review以外の全内容のhash**で、`published_at`を除く。review後に散文や機械値を書き換えるとpublishが落ちるので、reviewした内容とpublishされる内容が乖離しない。

reviewerはdraft、各lane thesis、proposalを読み、次を一つずつ確認する。

1. source freshness — `source_caveats`が実態を表しているか
2. shortlistの`research`確認事項が`answered` / `unresolved`として消化されているか
3. 一次情報へのtraceability — laneの主張がthesis sourceへ辿れるか
4. fact / estimate separation — 推定を観測として書いていないか
5. countercaseとunknownsが省略されていないか
6. scenarioとFVの整合 — `disposition_reason`が機械値と矛盾しないか
7. comparisonとportfolio fit — `comparison`がlane間の決め手を説明しているか
8. purchase method — `entry_timing`が直近dated catalystと整合し、event前に買う理由が根拠を持つか

`entry_timing`は購入提案がある場合に必須。選択銘柄の直近dated material event（決算、guidance更新等）と、そのeventの**前に**買う理由、またはeventが判断のload-bearingではない理由を書く。event結果が判断を変え得るのに先回りするなら、待つコストと先回りのriskの非対称性を明示する。published macro contextの`monitoring_points`にあるdated event（FOMC・BOJ会合・主要統計）と注文有効期間の位置関係も確認し、重なる場合はその扱いを1行書く。

修正が要るならdraftとその入力（thesis / proposal）へ戻る。修正後はhashが変わるので、`attempt`を増やして再度reviewする。

## 実行

```bash
# 1. 骨格を作る。数値はthesis/proposalから機械で埋まる
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-engine research assessment-scaffold \
  --assessment-id bargain-assessment-YYYYMMDD-<slug> \
  --asof YYYY-MM-DD \
  --shortlist-id <shortlist_id> \
  --thesis-id <thesis_id> --thesis-id <thesis_id> \
  --proposal-id <proposal_id> \
  --out .cache/opportunity/YYYY-MM-DD/bargain-assessment.yaml

# 2. 散文を記入したあと、束縛を検証して期待hashを読む
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-engine research assessment-publish \
  .cache/opportunity/YYYY-MM-DD/bargain-assessment.yaml --check

# 3. reviewerがreviewし、review欄とdraft_sha256を記入してからpublish
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-engine research assessment-publish \
  .cache/opportunity/YYYY-MM-DD/bargain-assessment.yaml
```

`no_actionable_bargain` / `defer`では`--proposal-id`を省略する。`--check`は`draft_sha256`と`review_binding`（`match` / `stale`）を印字するので、記入した内容に対する期待hashはここから取る。scaffoldが置く`draft_sha256`は全ゼロの番兵で実hashと衝突しないため、review欄を埋めないままpublishすると必ず落ちる。

## Storage and viewing

draftとworkspace上のcomparison / non-promoted thesisは`.cache`配下のephemeral artifactでcommitしない。publish後、canonical homeを持たないnon-promoted laneと調査全文だけをoperation sessionの`artifacts`へsnapshotする。promote済みthesis / reviewと作成済みproposalはIDと1〜3行の結果だけを`canonical_refs`へ置き、payloadを複製しない。

publish済みassessmentはBaibai Loopの`/stocks`にindexとして並び、`/stocks/assessments/{assessment_id}`が詳細を描画する。cloud配信は`views/assessment--{assessment_id}.json`をWorkerが`/api/assessments/{assessment_id}`へmapする。

broker操作とledger更新は人間の結果報告後だけ行う。
