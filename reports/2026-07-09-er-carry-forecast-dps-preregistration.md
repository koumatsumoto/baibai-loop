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

1. `docs/operations/decision-cycle.md` に、広域 triage は `--rules-path` で `research_selection_target_max` を引き上げた一時 rules を渡す手順を書く。
2. `docs/operations/decision-cycle.md` と `ai-value-bargain-selection` skill に、`dps_actual_annual / dps_forecast_annual > 1.5` の self-check を現在形で書く。
3. `macro-analysis` skill に、WebSearch は日本語 query で unavailable になりやすく、一次 URL 直接取得を優先しつつ必要時は英語 query を使う注意を fold する。
4. 記載は現状の手順と WHY に限り、2026-07 運用の経緯や作業ログを書かない。

## 5. 検証結果

事前登録後に baseline store と variant store を分けて構築した。baseline は現行 E[r] carry、variant は `dps_forecast_annual / close` を E[r] carry の配当成分に優先する実装である。

```bash
uv run baibai-loop-screening calibration-build --start 2022-09-01 --end 2026-03-31 --calibration-dir data/screening/calibration-er-carry-baseline --force
uv run baibai-loop-screening calibration-build --start 2022-09-01 --end 2026-03-31 --calibration-dir data/screening/calibration-er-carry-forecast-dps --force
uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-er-carry-baseline --horizon 6m --start 2022-09-01 --end 2024-06-30 --out .cache/er-carry-baseline-design-6m.yaml
uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-er-carry-baseline --horizon 6m --start 2024-07-01 --end 2026-03-31 --out .cache/er-carry-baseline-confirm-6m.yaml
uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-er-carry-forecast-dps --horizon 6m --start 2022-09-01 --end 2024-06-30 --out .cache/er-carry-forecast-dps-design-6m.yaml
uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-er-carry-forecast-dps --horizon 6m --start 2024-07-01 --end 2026-03-31 --out .cache/er-carry-forecast-dps-confirm-6m.yaml
```

| cohort | series | baseline median excess | forecast-DPS median excess | delta | baseline trap | forecast-DPS trap | delta |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| design | er_ranked_top10 | 8.483% | 5.709% | -2.773pt | 12.270% | 10.450% | -1.820pt |
| design | recommended_rank_top10 | 7.272% | 5.957% | -1.315pt | 11.200% | 11.410% | +0.210pt |
| confirm | er_ranked_top10 | 7.436% | 4.067% | -3.369pt | 10.560% | 16.110% | +5.550pt |
| confirm | recommended_rank_top10 | 9.750% | 3.593% | -6.157pt | 11.360% | 15.460% | +4.100pt |

`er_annual` axis の 6m mean rank IC は design 0.2262 → 0.2288、confirm 0.1477 → 0.1474 で大きくは崩れないが、replay 上位の median excess と trap 非悪化 gate を満たさない。したがって、forecast DPS を E[r] carry に広く優先する変更は不採用とする。

全面 forecast 優先の不採用後、原因に近い追加案として `dps_actual_annual / dps_forecast_annual > 1.5` の anomaly guard と、実績 DPS の split 正規化基準を `period_end` / `period_start` に寄せる variant も確認した。これらは事前登録外の post-hoc 追加確認であり、採用 judge の out-of-sample 性は持たない。結果は以下の通りで、いずれも 2.2 の非劣化 gate を満たさない。

| variant | cohort | er_ranked_top10 median delta | er_ranked_top10 trap delta | recommended_top10 median delta | recommended_top10 trap delta | 判定 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| anomaly guard | design | -2.088pt | +0.000pt | -1.022pt | +1.600pt | 不採用 |
| anomaly guard | confirm | -1.029pt | +1.660pt | -2.474pt | +1.080pt | 不採用 |
| period_end 正規化 | design | +0.000pt | +0.000pt | +0.000pt | +0.000pt | spot 不足 |
| period_end 正規化 | confirm | +0.000pt | +0.000pt | +0.000pt | +0.000pt | spot 不足 |
| period_start 正規化 | design | -0.898pt | +0.460pt | -0.972pt | +1.830pt | 不採用 |
| period_start 正規化 | confirm | -1.756pt | +2.770pt | -2.788pt | +1.730pt | 不採用 |

period_start 正規化は 2026-07-08 spot check では 4116 / 4008 / 8078 / 8273 の DPS 膨張を消したが、confirm replay の非劣化 gate を満たさないため採用しない。

## 6. 判定と採用範囲

#313 の E[r] carry 動作変更は、今回の改善ループでは採用しない。理由は、事前登録した forecast-DPS variant と追加確認した狭い variant が、design / confirm の replay 非劣化 gate を満たさないためである。#313 は close せず、次の改善候補として「配当スケジュールまたは corporate-action event を使った actual DPS の精密な asof-basis 化」を #319 で別途検討する。

同一 PR では、低リスクで運用価値がある以下だけを採用する。

- select recommendation summary に `dps_actual_annual` / `dps_forecast_annual` / `er_dividend_yield` を転記し、triage 時に実績・予想の乖離と E[r] carry 入力を目検できるようにする。
- decision cycleと`ai-value-bargain-selection` skillに、DPS 比 1.5 超の候補をforecast基準で読み替え、corporate action・特別配当・減配ガイダンスを確認するself-checkを現在形で記載する。
- `macro-analysis` skill に、WebSearch の日本語 query unavailable リスクと英語 query / 一次 URL 優先の操作注意を fold する。

## 7. 運用テスト

```bash
uv run pytest tests/test_screening_metrics.py tests/test_screening_estimates.py tests/test_screening_selection_liquidity.py
uv run baibai-loop-screening run --asof 2026-07-08 --output-path .cache/candidates-2026-07-08-output-fields.yaml --force
uv run baibai-loop-screening select --asof 2026-07-08 --candidates .cache/candidates-2026-07-08-output-fields.yaml --top 20 --detail full > .cache/select-2026-07-08-output-fields.yaml
```

- focused pytest: 39 passed。
- `run --asof 2026-07-08`: output は `.cache/candidates-2026-07-08-output-fields.yaml`。既存同様の partial warning（TTM quality / 入力欠損）で終了。
- `select --asof 2026-07-08`: output は `.cache/select-2026-07-08-output-fields.yaml`。full detail の `recommendations[].metrics` に `dps_actual_annual` / `dps_forecast_annual` / `er_dividend_yield` が出ることを確認した。
- temporary period_start variant の spot check: 4116 は actual DPS 220→55、4008 は 220→44、8078 は 290→58、8273 は 90→30 へ正規化され、carry 膨張は消える。ただし replay gate 不通過のため production には採用しない。
