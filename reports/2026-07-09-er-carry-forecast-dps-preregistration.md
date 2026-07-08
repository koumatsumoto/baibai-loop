# E[r] carry の forecast DPS 優先化 — 事前登録（#313 / #315）

本レポートは #313 の E[r] carry 改訂と #315 の triage 手順整備について、計測前に採否基準を固定する。git history 上の本節 commit を事前登録の正本とし、design/confirm 評価と採用後の運用テストは後続節に追記する。

## 0. 盲検性の限定

#313 の症状は 2026-07-06 / 2026-07-08 の本番 select で既に観測済みで、4116 / 4008 / 8078 / 8273 / 5108 などの `dps_actual_annual / dps_forecast_annual` 比が 1.5 を超え、実績 DPS ベースの carry が分割・減配ガイダンスをまたいで過大になることは既知である。したがって本検証は完全な事前盲検ではない。

本検証で新しく確認する対象は、`forecast DPS ÷ close` を E[r] carry の配当入力に使う変更が、既存の較正 replay 上で design / confirm の両窓とも非劣化かつ trap 非悪化に収まるかである。既知の異常銘柄の順位是正は意味論上の正しさとして扱い、成績指標は「正しい入力への変更が replay 品質を大きく壊さないこと」の検査に使う。

## 1. 仮説

E[r] は将来の年率リターン見積りなので、carry の配当成分は forward-looking な `dps_forecast_annual / close` を優先する。`dps_forecast_annual` が欠損または close が無効な場合だけ、実績配当利回り `dividend_yield` にフォールバックする。

一方、較正 replay の realized total return 近似に使う配当 accrual は過去に実際に支払われた配当の近似であり、`financial.dividend_yield` を維持する。つまり「E[r] carry の入力」と「realized return の配当 accrual」を実装上も出力上も分離する。

#315 は成績仮説ではなく運用手順のガードである。広域 triage、DPS 比 self-check、WebSearch 言語注意を現在形の手順として docs / skill に fold し、2026-07 運用で起きた迷いを次回の標準手順に落とす。

## 2. 採否基準

### 2.1 #313 semantic gate

以下をすべて満たすとき、意味論上の修正は通過とする。

1. `estimate_expected_return()` の carry 配当成分は `dps_forecast_annual / close` を優先する。
2. `dps_forecast_annual` が欠損、非正、または close が非正 / 欠損の場合は、現行の `financial.dividend_yield` にフォールバックする。
3. `ExpectedReturnEstimate.dividend_yield` は E[r] carry に使った配当利回りを返し、select recommendation では `dps_actual_annual` / `dps_forecast_annual` / `dividend_yield` を併記して、triage が実績・予想の乖離を目検できる。
4. 較正 evaluation の realized total return accrual は `PanelRow.dividend_yield` を使い続け、forecast DPS に置き換えない。

### 2.2 design / confirm 非劣化 gate

計測窓は既存の E[r] 主キー検証と同じく、6m horizon を一次基準、12m horizon を補助確認にする。

- design: `2022-09-01` 以上 `2024-06-30` 以下
- confirm: `2024-07-01` 以上 `2026-03-31` 以下

採用条件は以下の全条件を design / confirm の両方で満たすこと。

1. `er_ranked_top10` の 6m mean median excess が現行 baseline 比で **-1.0pt 以上**。
2. `recommended_rank_top10` の 6m mean median excess が現行 baseline 比で **-1.0pt 以上**。
3. `er_ranked_top10` と `recommended_rank_top10` の 6m mean trap rate が現行 baseline 比で **+1.0pt 以下**。
4. `er_annual` の 6m mean rank IC が正で、IC 正の cohort 率が **2/3 以上**。

12m は同方向かを確認するが、6m が semantic gate と非劣化 gate を満たす限り採否を覆さない。これは本変更の主目的が予測力の最適化ではなく、forward-looking E[r] 入力の corporate action 歪み除去だからである。

### 2.3 2026-07 運用 spot check

採用後の `select --asof 2026-07-08 --detail full` で以下を確認する。

1. #313 に挙がった高乖離銘柄（4116 / 4008 / 8078 / 8273 / 5108）の `er_carry_annual` が forecast DPS 基準で低下している。
2. recommendation summary に `dps_actual_annual` と `dps_forecast_annual` が出力され、DPS 比 1.5 超を triage で識別できる。
3. 上位候補が E[r] 降順を保ち、明らかな split / special dividend 起因の carry 膨張で top を占有しない。

## 3. 再現手順（計測前に固定）

baseline と variant は別 store に分け、store の混線を避ける。

```bash
uv run baibai-loop-screening calibration-build --start 2022-09-01 --end 2026-03-31 --calibration-dir data/screening/calibration-er-carry-baseline --force
uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-er-carry-baseline --horizon 6m --start 2022-09-01 --end 2024-06-30 --out .cache/er-carry-baseline-design-6m.yaml
uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-er-carry-baseline --horizon 6m --start 2024-07-01 --end 2026-03-31 --out .cache/er-carry-baseline-confirm-6m.yaml

uv run baibai-loop-screening calibration-build --start 2022-09-01 --end 2026-03-31 --calibration-dir data/screening/calibration-er-carry-forecast-dps --force
uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-er-carry-forecast-dps --horizon 6m --start 2022-09-01 --end 2024-06-30 --out .cache/er-carry-forecast-dps-design-6m.yaml
uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-er-carry-forecast-dps --horizon 6m --start 2024-07-01 --end 2026-03-31 --out .cache/er-carry-forecast-dps-confirm-6m.yaml
```

## 4. #315 完了条件

以下を同一 PR で満たす。

1. `docs/operations/monthly-cycle.md` に、広域 triage は `--rules-path` で `research_selection_target_max` を引き上げた一時 rules を渡す手順を書く。
2. `docs/operations/monthly-cycle.md` と `ai-value-bargain-selection` skill に、`dps_actual_annual / dps_forecast_annual > 1.5` の self-check を現在形で書く。
3. `macro-analysis` skill に、WebSearch は日本語 query で unavailable になりやすく、一次 URL 直接取得を優先しつつ必要時は英語 query を使う注意を fold する。
4. 記載は現状の手順と WHY に限り、2026-07 運用の経緯や作業ログを書かない。
