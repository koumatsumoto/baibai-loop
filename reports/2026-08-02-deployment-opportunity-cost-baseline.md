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

実行コマンド:

```bash
.venv/bin/python tools/measure_deployment_opportunity.py --out /tmp/deployment-opportunity.yaml
```

出力artifact SHA-256は`abe01e3a834a982ecc68967bee7105620e296caf9aa40965558622ebdc811d8c`。入力storeはapplication DB `d99b258ed4cb4c763d9bf154d792aef092618e2627d2e6afce4b1c665c5caa62`、run store `9792f62d8e0508ed16bf917aa97fdbd0a2240244d60f38800913e0075825839b`、market store `8d039f2219a981bb54dbbbdbbe098a7ad5efdcdbac077a80502cdae071c4ae70`だった。

### Coverage

- opportunity cycleは3件、結論unknownは0件。3件とも`no_action`だった。
- operation sessionの最初のrecordは2026-07-17。2026-05-01〜07-16はcycle recordが無いため、ledger executionからcycleを推定せずcoverage外とした。
- ledgerのopening balanceは2026-05-05。2026-05-01〜05-04はcapital coverage外とした。
- counterfactual算出不能は2/3 cycle。07-17と07-28のshortlistは`er_annual`焼き込み前で、束縛run revisionもprune済みだった。残存entryだけで順位を推定していない。

### Cycle別の結論・資本・counterfactual

| cycle as_of | 結論/source | cash total | deployed cost | deployment比率 | machine top-1 | forward return |
| --- | --- | ---: | ---: | ---: | --- | --- |
| 2026-07-17 | no_action / legacy operation result | 2,983,200円 | 1,887,900円 | 38.76% | 算出不能（E[r] 0/20） | `estimate_missing` |
| 2026-07-28 | no_action / bargain assessment | 2,983,200円 | 1,887,900円 | 38.76% | 算出不能（E[r] 0/20） | `estimate_missing` |
| 2026-07-29 | no_action / bargain assessment | 2,983,200円 | 1,887,900円 | 38.76% | 4849 | 3m/6m/1y/3yすべて未満期 |

07-29 cycleの最初の3m targetは2026-10-29。top-1 4849とpool 20件はいずれも現時点で`unresolved_future_horizon`であり、cash 0%との差もpool中央値との差もまだ算出しない。したがって、初回baselineから「no-actionが過剰慎重だった / 適正だった」のどちらも結論しない。

### Cash / deployment時系列

`cash total`はavailable + reserved。reservationは未投下cashとして分子へ入れず、book capitalの分母には含める。

| ledger event日（JST） | cash total | deployed cost | deployment比率 |
| --- | ---: | ---: | ---: |
| 2026-05-05 | 4,871,100円 | 0円 | 0.00% |
| 2026-05-07 | 4,277,400円 | 593,700円 | 12.19% |
| 2026-05-13 | 4,277,400円 | 593,700円 | 12.19% |
| 2026-05-14 | 4,074,400円 | 796,700円 | 16.36% |
| 2026-05-23 | 4,074,400円 | 796,700円 | 16.36% |
| 2026-05-25 | 3,946,900円 | 924,200円 | 18.97% |
| 2026-06-09 | 3,813,700円 | 1,057,400円 | 21.71% |
| 2026-06-16 | 3,579,700円 | 1,291,400円 | 26.51% |
| 2026-07-01 | 3,224,000円 | 1,647,100円 | 33.81% |
| 2026-07-03 | 3,224,000円 | 1,647,100円 | 33.81% |
| 2026-07-14 | 3,224,000円 | 1,647,100円 | 33.81% |
| 2026-07-15 | 2,983,200円 | 1,887,900円 | 38.76% |

deploymentはopening balance後の0%から38.76%まで段階的に上がり、記録済み3 cycleでは変化しなかった。これは投入量の記述であって運用成績ではなく、market valueや指数比較を含まない。

### 次回更新

四半期更新では同じコマンド・N・horizon・book-cost basisを維持し、新しいoperation cycleを追加する。2026-10-29以後に07-29 cycleの3mを初めて採点できる。焼き込み後のshortlistが増えるまでは、旧2 cycleの`estimate_missing`を復元・推定しない。
