---
name: position-review
description: 決算、material event、FV 到達、永久損失兆候、優れた代替を trigger として保有銘柄を再評価し、hold / add / reduce / exit を発行する。約定の記録は ledger-record skill。
---

# Position Review

対象 ticker だけを再評価する。含み損だけでは売らず、FV 到達を自動 exit にしない。action と税引後代替の算術は [`position-review.md`](../../../docs/reference/position-review.md) を正本とする。

## 手順

1. AGENTS.md に従い、対象tickerと判断基準日を指定して`position-review` sessionを開始する。active sessionがある場合はkind・ticker・as_ofが一致する同じrowだけを再開し、別対象なら停止する。`as_of`は以下の価格draftで使う最新完全営業日と揃える。

   ```bash
   uv run baibai-engine operation --db stores/application/baibai.sqlite \
     start --kind position-review --ticker <TICKER> --as-of <ASOF>
   ```
2. 最新完全営業日の market price draft を作り、人間確認後に apply する。

   ```bash
   uv run baibai-engine position market-price-draft --db stores/application/baibai.sqlite \
     --sqlite stores/market/market.sqlite --asof <ASOF> --out .cache/ledger/market-price-draft-<ASOF>.yaml
   uv run baibai-engine position apply-draft .cache/ledger/market-price-draft-<ASOF>.yaml \
     --db stores/application/baibai.sqlite --confirmed
   ```

3. `research position-prepare` で対象 ticker の workspace を作る。`thesis-scaffold` を作り、`research evaluate` で Thesis Review 要求以外の error を解消してから `review-scaffold` を実行する。
4. 直近 Thesis から変わった決算実数、guidance、資本政策だけを一次情報で更新する。Thesis の算術、source、Thesis Review、promote は `research` skill と [`thesis.md`](../../../docs/reference/thesis.md) に従う。guidance の据え置きは観測事実としない。当該四半期の経常利益 ÷ 通期 guidance を、過去3期の同四半期経常利益 ÷ 各期通期実績と比較して検証する。
5. Position Review draft を build し、load-bearing scalar、Thesis revision、ledger state、税引後代替価値を確認する。replacement Thesisと比較する場合は`--replacement-thesis-id`を指定する。人間が確認した後だけ publish する。

   ```bash
   uv run baibai-engine position position-review-build --db stores/application/baibai.sqlite \
     --thesis-id <THESIS_ID> --position-id <POSITION_ID> --out <draft>
   uv run baibai-engine position position-review --db stores/application/baibai.sqlite --input <draft>
   uv run baibai-engine position position-review publish <draft> \
     --db stores/application/baibai.sqlite --thesis-id <THESIS_ID>
   ```

6. 公開済みPosition Review IDを`canonical_refs`へ1件だけ入れ、reviewのartifact、actionを説明する`result`、次のtriggerの`next`、`human_confirmation.request/result`をOperationPayload fileへ記録して同じsessionをcompleteする。serviceはcanonical Position Reviewのticker・as_ofとsessionの一致を同じtransactionで検証する。未公開、別対象、別日付では完了しない。必要な監視はtaskへ記録する。`reduce / exit` は、人間から約定報告を受けた後に `ledger-record` skill へ進む。cloud反映は`ops-maintenance` skillに従う。

## 停止条件

次の場合は停止する。

- raw close、calendar coverage、corporate-action basis が unresolved である
- thesis revision と holding scalar が一致しない
- 人間が確認していない publish または売却記録を行おうとしている

## 正本

- action と算術: [`position-review.md`](../../../docs/reference/position-review.md)
- thesis: [`thesis.md`](../../../docs/reference/thesis.md)
- 資本規律: [`portfolio-management.md`](../../../docs/portfolio-management.md)
