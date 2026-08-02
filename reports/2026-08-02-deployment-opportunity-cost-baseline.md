# 人間ゲートの機会費用・資本deployment baseline

価値tier: T1 — no-action / defer時のmachine counterfactualと資本deployment paceを同じ時計で観測し、資本を投じない慎重さの実コストを判断できるようにする。

## 事前登録

この節はcounterfactual returnと資本時系列の算出前に固定する。初回対象期間は2026-05-01以降、母集団はapplication DBに存在する`session_kind: opportunity`の全operation sessionとする。completedだけに絞らず、結論不明・shortlist未束縛・counterfactual算出不能も1 cycleとして出力し、無言で落とさない。operation session導入前の期間は、ledger executionからcycleを推定せず、cycle record coverage外の期間として明示する。

### Cycle結論

1. operationがcanonical refで束縛するbargain assessmentが1件なら、その`result`を`proposal → deploy`、`defer → defer`、`no_actionable_bargain → no_action`へ写す。
2. assessment導入前だけ、operationのcanonical `payload.result`先頭に明記された`deploy` / `defer` / `no actionable bargain` / `no_actionable_bargain`を同じ3分類へ写す。
3. 未定義、複数assessment、active operationは`unknown`とし、cycle行を残す。日付一致だけでassessmentやshortlistを推定しない。

### Machine counterfactual

- `top-N`は**N=1**に固定する。1回の通常買付けと同じ単一銘柄counterfactualとし、複数銘柄の配分を後付けで仮定しない。
- shortlist全entryをpublish時の`er_annual`降順（同値はticker昇順）に並べる。焼き込み前shortlistは、束縛run revisionがrun storeに残る場合だけ同じE[r]を補完する。全entryのE[r]が揃わなければ`estimate_missing`とし、残った銘柄だけで順位を推定しない。
- horizonは既存`shortlist outcome`と同じ`3m / 6m / 1y / 3y`、basisはprice-only。forward returnの価格解決は同じ`compute_forward_returns`を使う。
- 主benchmarkはcash 0%、副benchmarkはshortlist poolの同horizon中央値。machine top-1が未解決ならreturnを0%へ補完せず`unresolved`とし、理由と件数を残す。
- 非ランダム割当、少数cycle、forward窓の重複があるため記述比較に限定し、因果効果やtrack recordを主張しない。

### 資本deployment pace

- sourceはcanonical ledger eventだけとし、各cycleの観測時点はcompletedなら`completed_at`、activeなら`started_at`とする。
- `deployed_cost_yen`はその時点までのexecutionをFIFO replayした未決済lotの取得原価、`cash_total_yen`は`available_cash_yen + reserved_cash_yen`とする。
- `book_capital_yen = deployed_cost_yen + cash_total_yen`、`deployment_ratio_pct = deployed_cost_yen / book_capital_yen × 100`。市場評価益をdeployment量へ混ぜず、reserved cashは未投下cashとして別掲したうえで分母へ含める。
- cash時系列は2026-05-01以降にledger eventがある各JST日の日末状態を出す。イベントの無い日を補間しない。

### 報告形式と判断境界

初回および四半期更新は、(i) cycle別の結論・資本状態・machine top-1、(ii) horizon別returnとcash/pool比較、(iii) ledger event日別のcash/deployment時系列、(iv) coverage gapを同じ構造で出す。単一の「機会損失額」へ畳まず、自動deploy、目標deployment比率、OP3 gate、E[r]、rankingは変更しない。

## 初回結果

計測実装後に追記する。
