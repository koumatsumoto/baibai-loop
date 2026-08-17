---
title: "自己レンジ anchor を真の倍率履歴と比べる — 事前登録"
summary: "価格履歴を倍率の単位で表した現行 anchor と、cohort を横に並べて作る真の倍率履歴 anchor を、結果を見る前に固定した窓・母集団・閾値で比べる。"
doc_type: measurement-record
status: active
date: 2026-08-17
---

# 自己レンジ anchor を真の倍率履歴と比べる — 事前登録

価値tier: T1 — E[r] はランキングの軸そのもので、その reversion 成分は 44.6% の銘柄で「倍率の平均回帰」ではなく「価格の平均回帰」を測っている。どちらが良いかは実測でしか決まらない。

## 1. 固定する問いと非盲検性

機械 E[r] の FV anchor は `min(sector 中央値倍率, 自己レンジ中央値倍率)` である。`_valuation_history` は fundamentals を最新値で固定して調整後終値だけを動かすので、**自己レンジ側は倍率の履歴ではなく価格の履歴**であり、価格比例の軸では `自己中央値 / 現値` が軸によらず `median(750 営業日終値) / 現値` に一致する（[`valuation-metrics.md` §6](../../../docs/reference/valuation-metrics.md)）。

**著者は盲検ではない。** issue #910 の集計で以下を既に見ている。

- 価格比例の全軸で `self_range_median / current` が完全一致（n=3,424、p50・p90・p99 とも差 0.000000%）
- 自己レンジ側が binding するのは軸評価 6,990 件のうち 59.5%、銘柄単位では 44.6% が全軸で自己レンジ binding、うち 38.0% は**同じ数を 2 回平均**している
- panel 110,129 行の 83.55% が負の implied upside、20.09% が ±50% cap に張り付く

これらは**現行 anchor が何を測っているかの記述**であり、真の倍率履歴の方が良いかについては何も言っていない。価格の平均回帰は momentum reversal として実測 IC を持ちうるし、成長企業を機械的に減点する向きが結果として保守側に働く可能性もある。本書を commit するまで、倍率履歴 anchor から作った E[r] を forward return と結んだ IC・分位別実現・trap を一切計測しない。cohort 別の履歴充足率、2 つの anchor の相関、binding 側の分布は outcome-free として確認してよい。

この study が答える問いは「reversion の anchor を価格履歴と倍率履歴のどちらで作るか」である。`min` を平均へ変えること、`UPSIDE_CAP`、`REALIZATION_RATE_ANNUAL` は評価しない（issue #910 のスコープ外指定に従う）。

## 2. 真の倍率履歴 anchor の作り方

較正 panel は cohort ごとに point-in-time の倍率を持つ。**cohort を横に並べたものを倍率履歴とする。**

- 窓は as-of 直前の **36 cohort**（月次なので約 3 年。現行の 750 営業日窓に対応する）
- 最低本数は **24 本**（窓の 2/3）。現行実装の下限は 750 本中 100 本＝13% だが、月次標本で 13%（5 本）の中央値は anchor として不安定なので、月次側は厳しく取る。**この閾値は結果を見る前に決め、事後に動かさない**
- 軸は production と同じ 2 系統。収益 anchor は `per_forward`、無ければ `per_trailing`。資産 anchor は `pbr`
- `self_multiple_median` = その軸の履歴中央値。sector 側は panel の `smg_*`（= `current / sector_median - 1`）から `sector_median = current / (1 + smg)` として復元する
- anchor = `min(sector_median, self_multiple_median)`、implied upside = `anchor / current - 1`
- 2 系統の upside を平均し、`reversion = 0.10 × clip(upside, ±0.50)`
- `E[r]_history = reversion + panel の er_carry_annual`（carry は本 study の対象外なので現行値を使う）

## 3. 比較の母集団

両方の E[r] が出る行に限る。現行 `er_annual` が null の行、倍率履歴が 24 本に満たない行は判定に使わず、件数だけ併記する。

水平線は 3y と 5y。cohort を時間で design / confirm に 2 分割し、[`estimate-calibration.md`](../../../docs/reference/estimate-calibration.md) の「両方で同方向・基準充足のときだけ採用。片側のみは不確定、両側逆は棄却」に従う。

## 4. 仮説と採否

| | 内容 |
| --- | --- |
| H1 | 倍率履歴 anchor の E[r] は現行より forward 実現をよく説明する |
| H2 | どちらでもない。現行を維持する |

**採否基準**

- **rank IC が両窓・両水平線で現行を上回ること。** 1 つでも下回れば採用しない
- 分位差でも符号が一致すること
- `dividend_yield` 層別 control で trap が悪化しないこと
- 上記を満たさなければ H2 とし、記述だけを残す

## 5. 停止条件

- 履歴が 24 本に満たない行が母集団の 50% を超える水平線では判定せず、件数だけ報告する
- 2 つの anchor から出た implied upside の順位相関が 0.95 を超える場合、比較する意味が無いので H2 とする
- confirm 窓の cohort が 0 件の水平線は「確認不能」と明記し、design 単独で採用しない

## 6. 解釈の限界

- 月次 cohort の中央値は 750 営業日の中央値より粗い。**本 study が測るのは「倍率履歴 anchor が現行より良いか」であって、月次近似が日次倍率履歴の代替になるかではない**。採用が示唆された場合、日次倍率履歴の実装可否は別途評価する
- panel の倍率はその cohort の screening rules で作られている。rules が動いた期間を跨ぐと履歴の意味が揃わない。`rules_hash` が全 cohort で同一であることを計測前に確認し、違えば窓を分ける
- sector median を `smg` から復元するので、`smg` が null の行では sector 側を欠損として扱う。現行実装が sector 側を持つ行との差が出るなら件数で示す
