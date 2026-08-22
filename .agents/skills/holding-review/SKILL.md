---
name: holding-review
description: 決算、material event、FV 到達、永久損失兆候、優れた代替を trigger に保有銘柄を再評価し、hold / add / reduce / exit を発行する。約定記録は ledger-record skill。
---

# Holding Review

対象 ticker だけを再評価する。含み損だけでは売らず、FV 到達を自動 exit にしない。action と税引後代替の正本は [`holding-review.md`](../../../docs/reference/holding-review.md)。

## 手順

1. AGENTS.md に従い `earnings-material-event` session を start または resume する。
2. 最新完全営業日の market price draft を作り、人間確認後に apply する。

   ```bash
   uv run baibai-engine position market-price-draft --db stores/application/baibai.sqlite \
     --sqlite stores/market/market.sqlite --asof <ASOF> --out .cache/ledger/market-price-draft-<ASOF>.yaml
   uv run baibai-engine position apply-draft .cache/ledger/market-price-draft-<ASOF>.yaml \
     --db stores/application/baibai.sqlite --confirmed
   ```

3. `research holding-prepare` で対象 ticker の workspace を作り、`thesis-scaffold` と `review-scaffold` を実行する。
4. 直近 thesis から変わった決算実数、guidance、資本政策だけを一次情報で更新する。thesis の算術・source・独立反証・promote は `research` skill と [`thesis.md`](../../../docs/reference/thesis.md) に従う。guidance 据え置きは観測事実とせず、同四半期の進捗を過去3期の同四半期対通期実績と比較して検証する。
5. review draft を build し、load-bearing scalar、thesis revision、ledger state、税引後代替価値を確認する。人間確認後だけ publish する。

   ```bash
   uv run baibai-engine position holding-review-build --db stores/application/baibai.sqlite \
     --thesis-id <THESIS_ID> --position-id <POSITION_ID> --out <draft>
   uv run baibai-engine position holding-review --db stores/application/baibai.sqlite --input <draft>
   uv run baibai-engine position holding-review publish <draft> \
     --db stores/application/baibai.sqlite --thesis-id <THESIS_ID>
   ```

6. review ID、action、次の trigger を session と task に記録して complete する。`reduce / exit` は人間の約定報告後に `ledger-record` skill へ進む。cloud 反映は `ops-maintenance` skill に従う。

## 停止条件

- raw close、calendar coverage、corporate-action basis が unresolved。
- thesis revision と holding scalar が不一致。
- 人間確認のない publish または売却記録。

## 正本

- action と算術: [`holding-review.md`](../../../docs/reference/holding-review.md)
- thesis: [`thesis.md`](../../../docs/reference/thesis.md)
- 資本規律: [`portfolio-management.md`](../../../docs/portfolio-management.md)
