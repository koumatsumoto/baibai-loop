# actual DPS の asof-basis 補正設計 — 事前登録（#319）

本レポートは #319 の actual DPS asof-basis 補正について、計測前に仮説・採否基準・再現手順を固定する。git history 上の本節 commit を事前登録の正本とし、design / confirm 検証と採用可否は後続節へ追記する。

## 0. 盲検性の限定

#313 / #320 で、次は既知である。

- 2026-07-06 / 2026-07-08 の select では、4116 / 4008 / 8078 / 8273 などの `dps_actual_annual` が決算期跨ぎの株式分割により旧株式基準のまま残り、`dividend_yield` と E[r] carry を膨張させた。
- `dps_forecast_annual / close` を E[r] carry に全面優先する案は、事前登録した design / confirm replay の非劣化 gate を満たさず不採用になった。
- post-hoc で確認した `dps_actual_annual / dps_forecast_annual > 1.5` guard と、実績 DPS の `period_start` 一律正規化も、replay gate を満たさなかった。ただし `period_start` 正規化は 2026-07-08 spot では 4116 / 4008 / 8078 / 8273 の DPS 膨張を消した。

したがって本検証は完全な事前盲検ではない。本検証で新しく確認する対象は、「一律 period_start 正規化」ではなく、corporate action と forecast DPS の両方で基準混在を確認できる実績 DPS だけを狭く asof-basis 補正する変更が、design / confirm の両窓で replay 品質を大きく壊さず、既知の split artifact を説明できるかである。

## 1. 仮説

実績年間 DPS (`DivAnn`) は、会計上は実際に各基準で支払われた per-share 配当額の合計であり、EPS/BPS のように常に分割遡及されるとは限らない。一方、screening / E[r] / calibration panel では、現在株価と割るため、DPS も asof 時点の株式基準へ揃える必要がある。

現行実装は financial summary の `disclosed_at` より後・asof 以前の `adjustment_factor` だけを実績 DPS に掛ける。これは「開示後に分割が起きた」場合には正しいが、「分割が決算期末付近に起き、FY 開示は分割後」という #313 型では補正できない。

本仮説では、実績 DPS の補正を次のように限定する。

1. 通常は現行の `disclosed_at` 基準の asof-basis 正規化を維持する。
2. 実績 DPS を持つ FY 行について、`period_start` より後・asof 以前に分割/併合 (`adjustment_factor != 1`) があり、かつ `period_start` 基準で正規化した DPS が直近 forecast DPS に明確に近づく場合だけ、実績 DPS の補正基準を `period_start` へ拡張する。
3. forecast DPS は採用値そのものには使わず、基準混在の確認にだけ使う。forecast DPS が欠損・非正の場合はこの拡張補正を行わない。
4. 補正対象は actual DPS の asof-basis 化であり、forecast DPS を E[r] carry の代替入力にはしない。

この設計は、配当支払日・権利確定日が現行 local schema に無い制約下で、観測可能な corporate action event と進行期 forecast DPS を組み合わせて、旧株式基準の actual DPS だけを狭く直すものである。配当スケジュールを直接取得できる provider を後日追加できる場合は、本設計よりそちらを優先する。

## 2. 採否基準

### 2.1 semantic gate

以下をすべて満たすとき、意味論上の修正は通過とする。

1. `FinancialSnapshot.dps_actual_annual` と `dividend_yield` は、現在株価と同じ asof 株式基準の DPS を表す。
2. `disclosed_at` より後の分割/併合は従来どおり補正される。
3. `disclosed_at` より前でも、FY 実績 DPS の対象期間中に分割/併合があり、`period_start` 基準の補正値が forecast DPS に近づく場合だけ、actual DPS を追加補正する。
4. 実績 DPS の raw 値が高いだけ、または減配ガイダンスで forecast DPS が低いだけのケースを、分割 artifact と誤認して補正しない。
5. `estimate_expected_return()` は引き続き `financial.dividend_yield` を carry 配当成分に使い、forecast DPS への全面置換を行わない。
6. calibration evaluation の realized total return accrual は `PanelRow.dividend_yield` を使い続ける。ただしその `dividend_yield` は actual DPS の asof-basis 補正後の値になる。

実装上の確認条件は以下とする。

- `period_start` 基準の factor が `disclosed_at` 基準の factor と異なる場合だけ候補にする。
- raw actual DPS と forecast DPS の乖離が大きく、補正後 actual DPS が forecast DPS に近づく場合だけ候補にする。
- 補正後 actual DPS / forecast DPS が極端に外れる場合は補正しない。
- 上記の閾値は検証前に実装内で固定し、grid search しない。

### 2.2 design / confirm 非劣化 gate

計測窓は #320 と同じく、6m horizon を一次基準、12m horizon を補助確認にする。

- design: `2022-09-01` 以上 `2024-06-30` 以下
- confirm: `2024-07-01` 以上 `2026-03-31` 以下

採用条件は以下の全条件を design / confirm の両方で満たすこと。

1. `er_ranked_top10` の 6m mean median excess が現行 baseline 比で **-1.0pt 以上**。
2. `recommended_rank_top10` の 6m mean median excess が現行 baseline 比で **-1.0pt 以上**。
3. `er_ranked_top10` と `recommended_rank_top10` の 6m mean trap rate が現行 baseline 比で **+1.0pt 以下**。
4. `er_annual` の 6m mean rank IC が正で、IC 正の cohort 率が **2/3 以上**。

12m は同方向かを確認するが、6m が semantic gate と非劣化 gate を満たす限り採否を覆さない。有意性は主張せず、effect size と cohort 勝率だけを見る。

### 2.3 2026-07 spot check

採用候補実装で `select --asof 2026-07-08 --detail full` を実行し、以下を確認する。

1. #313 に挙がった 4116 / 4008 / 8078 / 8273 の `dps_actual_annual` と `er_dividend_yield` が分割後基準へ低下する。
2. 5410 のように減配ガイダンスで actual / forecast が乖離しているだけのケースを、分割 artifact として補正しない。
3. recommendation summary に `dps_actual_annual` / `dps_forecast_annual` / `dividend_yield` / `er_dividend_yield` が出力され、triage self-check に必要な情報が残る。
4. 上位候補が明らかな split / special dividend 起因の carry 膨張で占有されない。

## 3. 再現手順（計測前に固定）

baseline と variant は別 store に分け、store の混線を避ける。variant 実装後に以下を実行する。

```bash
uv run baibai-loop-screening calibration-build --start 2022-09-01 --end 2026-03-31 --calibration-dir data/screening/calibration-dps-asof-baseline --force
uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-dps-asof-baseline --horizon 6m --start 2022-09-01 --end 2024-06-30 --out .cache/dps-asof-baseline-design-6m.yaml
uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-dps-asof-baseline --horizon 6m --start 2024-07-01 --end 2026-03-31 --out .cache/dps-asof-baseline-confirm-6m.yaml
uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-dps-asof-baseline --horizon 12m --start 2022-09-01 --end 2024-06-30 --out .cache/dps-asof-baseline-design-12m.yaml
uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-dps-asof-baseline --horizon 12m --start 2024-07-01 --end 2026-03-31 --out .cache/dps-asof-baseline-confirm-12m.yaml

uv run baibai-loop-screening calibration-build --start 2022-09-01 --end 2026-03-31 --calibration-dir data/screening/calibration-dps-asof-confirmed --force
uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-dps-asof-confirmed --horizon 6m --start 2022-09-01 --end 2024-06-30 --out .cache/dps-asof-confirmed-design-6m.yaml
uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-dps-asof-confirmed --horizon 6m --start 2024-07-01 --end 2026-03-31 --out .cache/dps-asof-confirmed-confirm-6m.yaml
uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-dps-asof-confirmed --horizon 12m --start 2022-09-01 --end 2024-06-30 --out .cache/dps-asof-confirmed-design-12m.yaml
uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-dps-asof-confirmed --horizon 12m --start 2024-07-01 --end 2026-03-31 --out .cache/dps-asof-confirmed-confirm-12m.yaml
```

## 4. 運用テスト

採用する場合は、本番 store 再構築前に現 asof で pipeline を回し、実出力を spot check する。

```bash
uv run baibai-loop-screening run --asof 2026-07-08 --output-path .cache/candidates-2026-07-08-dps-asof.yaml --force
uv run baibai-loop-screening select --asof 2026-07-08 --candidates .cache/candidates-2026-07-08-dps-asof.yaml --top 20 --detail full > .cache/select-2026-07-08-dps-asof.yaml
```

## 5. 判定記録

## 5. 検証結果

事前登録後に、baseline store と variant store を分けて構築した。baseline は現行 `disclosed_at` 基準の actual DPS 正規化、variant は FY 行の actual DPS だけを、次の固定条件をすべて満たす場合に `period_start` 基準へ拡張補正する実装である。

- `period_start` 基準の factor が `disclosed_at` 基準の factor と異なる。
- `dps_actual_annual / dps_forecast_annual >= 1.5`。
- 補正後 actual DPS / forecast DPS が `0.5..1.5` の範囲に入る。
- 補正後 actual DPS が raw actual DPS より forecast DPS に近づく。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-screening calibration-build --start 2022-09-01 --end 2026-03-31 --calibration-dir data/screening/calibration-dps-asof-baseline --force
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-screening calibration-build --start 2022-09-01 --end 2026-03-31 --calibration-dir data/screening/calibration-dps-asof-confirmed --force
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-dps-asof-baseline --horizon 6m --start 2022-09-01 --end 2024-06-30 --out .cache/dps-asof-baseline-design-6m.yaml
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-dps-asof-baseline --horizon 6m --start 2024-07-01 --end 2026-03-31 --out .cache/dps-asof-baseline-confirm-6m.yaml
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-dps-asof-confirmed --horizon 6m --start 2022-09-01 --end 2024-06-30 --out .cache/dps-asof-confirmed-design-6m.yaml
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-dps-asof-confirmed --horizon 6m --start 2024-07-01 --end 2026-03-31 --out .cache/dps-asof-confirmed-confirm-6m.yaml
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-dps-asof-baseline --horizon 12m --start 2022-09-01 --end 2024-06-30 --out .cache/dps-asof-baseline-design-12m.yaml
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-dps-asof-baseline --horizon 12m --start 2024-07-01 --end 2026-03-31 --out .cache/dps-asof-baseline-confirm-12m.yaml
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-dps-asof-confirmed --horizon 12m --start 2022-09-01 --end 2024-06-30 --out .cache/dps-asof-confirmed-design-12m.yaml
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-screening calibration-evaluate --calibration-dir data/screening/calibration-dps-asof-confirmed --horizon 12m --start 2024-07-01 --end 2026-03-31 --out .cache/dps-asof-confirmed-confirm-12m.yaml
```

### 5.1 6m primary gate

| cohort | series | baseline median excess | variant median excess | delta | baseline trap | variant trap | delta |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| design | er_ranked_top10 | 8.483% | 7.680% | -0.803pt | 12.270% | 12.730% | +0.460pt |
| design | recommended_rank_top10 | 14.113% | 13.185% | -0.928pt | 8.640% | 9.550% | +0.910pt |
| confirm | er_ranked_top10 | 7.436% | 6.792% | -0.645pt | 10.560% | 13.330% | +2.770pt |
| confirm | recommended_rank_top10 | 6.687% | 3.856% | -2.831pt | 5.560% | 9.440% | +3.880pt |

`er_annual` axis の mean rank IC は design 0.2262 → 0.2254、confirm 0.1477 → 0.1451 で正を維持し、variant の IC 正 cohort 率は design 1.000、confirm 0.944 だった。したがって axis IC 条件は満たす。

一方、6m primary gate は confirm の trap 非悪化条件と `recommended_rank_top10` の median 非劣化条件を満たさない。confirm `recommended_rank_top10` は median -2.831pt、trap +3.880pt で、事前登録した許容幅（median -1.0pt 以上、trap +1.0pt 以下）を明確に超えて悪化した。

### 5.2 12m 補助確認

| cohort | series | baseline median excess | variant median excess | delta | baseline trap | variant trap | delta |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| design | er_ranked_top10 | 20.354% | 18.274% | -2.080pt | 15.910% | 16.820% | +0.910pt |
| design | recommended_rank_top10 | 30.089% | 28.790% | -1.299pt | 11.360% | 13.180% | +1.820pt |
| confirm | er_ranked_top10 | 15.179% | 5.603% | -9.576pt | 16.670% | 25.000% | +8.330pt |
| confirm | recommended_rank_top10 | 14.581% | 5.711% | -8.870pt | 8.330% | 19.170% | +10.840pt |

12m は採否の一次基準ではないが、confirm で同方向に大きく悪化している。6m gate 不通過を覆す材料はない。

## 6. 2026-07 spot check

variant 実装で 2026-07-08 asof の pipeline を回した。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-screening run --asof 2026-07-08 --output-path .cache/candidates-2026-07-08-dps-asof.yaml --force
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-screening select --asof 2026-07-08 --candidates .cache/candidates-2026-07-08-dps-asof.yaml --top 20 --detail full > .cache/select-2026-07-08-dps-asof.yaml
```

`run` は既存同様の partial warning（TTM quality / 入力欠損）で exit 2 だが、output は作成され、select は正常終了した。

| ticker | baseline actual / forecast DPS | variant actual / forecast DPS | baseline E[r] | variant E[r] | 判定 |
| --- | ---: | ---: | ---: | ---: | --- |
| 4116 | 220 / 55 | 55 / 55 | 17.54% | 2.22% | 分割 artifact は解消 |
| 4008 | 220 / 48 | 44 / 48 | 14.83% | 1.16% | 分割 artifact は解消 |
| 8078 | 290 / 66 | 58 / 66 | 12.64% | -0.59% | 分割 artifact は解消 |
| 8273 | 90 / 30 | 30 / 30 | 9.90% | 3.90% | 分割 artifact は解消 |
| 5108 | 230 / 125 | 115 / 125 | 9.85% | 6.67% | 分割 artifact は解消 |
| 5410 | 180 / 100 | 180 / 100 | 11.51% | 11.51% | 減配ガイダンス乖離は補正せず |

spot では #313 型の split artifact は説明でき、5410 型の単純な actual / forecast 乖離は補正しなかった。variant select top は 6417 / 5410 / 7095 / 5445 / 4887 となり、4116 / 4008 / 8078 の carry 膨張による上位占有は消えた。

## 7. 判定

本 variant は採用しない。

理由は、事前登録した 6m design / confirm 非劣化 gate を満たさないためである。spot check では既知の 2026-07 split artifact を解消するが、confirm replay の `recommended_rank_top10` で median -2.831pt、trap +3.880pt まで悪化し、12m 補助確認でも confirm が同方向に悪化した。grid search や閾値の後出し調整は行わない。

現行 production code には actual DPS の新しい補正を入れない。引き続き monthly-cycle の triage self-check（`dps_actual_annual / dps_forecast_annual > 1.5` の候補を corporate action / 特別配当 / 減配ガイダンスで確認し、必要なら E[r] を手で読み替える）を使う。

## 8. 残課題

現行 local schema には配当支払日・権利確定日・配当基準日が無く、観測できる corporate action は daily bar の `adjustment_factor` だけである。この制約下の heuristic は replay gate を満たさなかった。actual DPS を機械的に asof-basis 化するなら、配当スケジュールまたは corporate-action event と配当基準日の対応を一次データで持つ provider を追加してから、別 issue として再検証する。
