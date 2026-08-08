---
name: holding-review
description: 保有銘柄の見直し。決算・material event・FV 到達・永久損失兆候・優れた代替候補を trigger に、hold / add / reduce / exit を判定して publish する。売却の記録は ledger-record skill。
---

# Holding Review

対象 ticker だけを見直す（全銘柄 screening はしない）。含み損だけでは売らない。FV 到達は review trigger であって自動 exit ではない。action の意味と税引後代替の算術は [`holding-review.md`](../../../docs/reference/holding-review.md) が正本。

## 手順

1. AGENTS.md の session 規約に従い `operation start --kind earnings-material-event --as-of <日付>`。
2. **market price を先に固定する**:

   ```bash
   uv run baibai-engine position market-price-draft --db stores/application/baibai.sqlite \
     --sqlite stores/market/market.sqlite --asof <最新完全営業日> --out .cache/ledger/market-price-draft-<ASOF>.yaml
   uv run baibai-engine position apply-draft .cache/ledger/market-price-draft-<ASOF>.yaml --db stores/application/baibai.sqlite --confirmed
   ```

3. **workspace**（保有 lane は screening selection を要求しない）:

   ```bash
   uv run baibai-engine research holding-prepare --db stores/application/baibai.sqlite --asof <ASOF> \
     --ticker XXXX --workspace .cache/opportunity/<ASOF>/holding-XXXX
   uv run baibai-engine research thesis-scaffold --workspace <同上> --db stores/application/baibai.sqlite \
     --ticker XXXX --sqlite-path stores/market/market.sqlite --target-session <次session>
   uv run baibai-engine research review-scaffold --workspace <同上> --db stores/application/baibai.sqlite --ticker XXXX
   ```

4. 一次情報の **material delta だけ**を更新する（決算実数・guidance・資本政策。thesis 執筆規約と機械 gate は research skill 手順 4〜5 と同じ）。独立反証を通して promote する。

   「guidance 据え置き」は会社の主張であって観測ではない。反証は季節進捗で取る: 当該四半期の経常利益 ÷ 通期 guidance を、過去 3 期の同四半期 ÷ その期の通期**実績**と比べる（`jquants_fin_summaries` だけで出る）。乖離があれば、会社が織り込み済みなのか未達なのかを一次開示の定性説明で切り分けてから scenario の starting earnings に使う。
5. **review の構築と publish**（人間確認後だけ publish）:

   ```bash
   uv run baibai-engine position holding-review-build --db stores/application/baibai.sqlite \
     --thesis-id <THESIS_ID> --position-id <POSITION_ID> --out .cache/holding-review/review-XXXX-<ASOF>.yaml
   uv run baibai-engine position holding-review --db stores/application/baibai.sqlite --input .cache/holding-review/review-XXXX-<ASOF>.yaml
   uv run baibai-engine position holding-review publish .cache/holding-review/review-XXXX-<ASOF>.yaml \
     --db stores/application/baibai.sqlite --thesis-id <THESIS_ID>
   ```

   load-bearing scalar・thesis revision・ledger state・税引後代替価値を publish 前に検証する。
6. `reduce / exit` 判定なら、人間の発注・約定報告を待って `ledger-record` skill（sell-result-draft）へ。判定にかかわらず review ID と action を session へ記録し、次の trigger（次回決算・FV 水準）を task 化して complete する。cloud 反映（push-app → materialize）。

## 停止条件

- raw close・calendar coverage・corporate action basis が unresolved。
- thesis revision と holding scalar が不一致（build が fail-closed する）。
- 人間確認の無い publish・売却記録。
