---
title: "Estimate calibration"
summary: "point-in-time panelと長期forward returnでE[r]・FV・selection方法を較正するcontract。"
doc_type: reference
status: active
last_reviewed: 2026-07-13
---

# estimate-calibration

screening の機械見積りと選定順位を過去 as-of で再構成し、価格リターンの実現値へ突き合わせる local-only の較正処理である。portfolio outcome や JPX total-return benchmark とは別の、cross-sectional な estimator diagnostic を所有する。

## Horizon authority

| horizon | target | authority |
| --- | --- | --- |
| `3m` / `6m` | calendar month addition | regression alert |
| `1y` | calendar year addition | leading evidence |
| `3y` / `5y` | calendar year addition | production decision evidence |

`calibration-evaluate` の通常実行は全 horizon を diagnostic として出力する。実証的な screen、ranking、E[r] policy parameter の変更候補は、`--run-purpose production_decision` で明示した required as-of と required metric に対して、3y と 5y の双方が eligible のときだけ検討できる。artifact は設定やコードを自動変更しない。

target は cohort の actual as-of date に calendar month を加算する。元の日が calendar month-end の場合は対象月末を保ち、非取引日は target 以下の最終取引日に解決する。

## Data integrity

panel は cohort as-of 以下の最新 `eq_master` snapshot だけを読む。prior snapshot、snapshot unavailable、survivorship、delisting、corporate-action event coverage の不備は payload に残り、3y/5y evidence を block する。

forward row は `resolved` または明示的な unresolved status を持つ。target と entry はそれぞれ target/as-of 以下の最終取引日で解決し、15 日超の stale exit は resolved return に入れない。価格は as-of basis adjustment factor で正規化するが、metric basis は `price_return_only` であり配当 accrual を加えない。entry 時点の配当利回りを horizon 年数で按分する固定 accrual は、期間中の増配・減配・無配・支払時期を観測した実現配当ではないため、実現値として扱わない。

<a id="coverage-verdicts"></a>

### Coverage 判定の導出

3 つの coverage は cache された観測から評価時に導出する。cohort が書かれた時点の契約ではなく現行契約で判定するためであり、既存 cohort も再構築せずに判定し直せる。

| coverage | 測るもの | `complete` の条件 |
| --- | --- | --- |
| survivorship | panel の population が as-of の投資可能 universe を再現しているか | as-of に価格が付いていた銘柄すべてが master read に含まれる（`asof_population_mismatch_count == 0`） |
| delisting | 窓中に価格が途切れた銘柄に exit value があるか | 窓中に系列が終わる銘柄が無い（`unpriced_exit_count == 0`） |
| corporate action | 価格系列が価格を動かした action を反映しているか | 全 bar に分割調整 factor があり、窓中に上場終了が無い |

survivorship は population の性質なので panel が測り、cohort はその結果を読む。bar store は市場から消えた銘柄の価格も保持するため、as-of に価格が付いていた集合を master snapshot と独立に観測できる。master が as-of より後なら当時上場していて現在は廃止された銘柄を欠き、前なら以降に上場した銘柄を欠く。どちらも断面が as-of の投資可能 universe ではないので incomplete とし、exact-date master は定義上 0 にする。計測前に書かれた panel は件数を null として報告する（未計測を「欠けなし」と読めないようにする）。

corporate action の `complete` は「ローカルに検出できる未対応 action が無い」ことを意味する。系列を調整もせず上場も終えない action（株主割当増資など）はローカルに source が無く、この残余は判定に含まれない。上場終了は合併・株式交換の class にあたり、J-Quants が対価を調整しないと明示しているため `terminated_listing` として factor 欠落と区別する。

### 未解決 row の分類

未解決 row は 1 種類の欠陥ではないので、authority gate は総数ではなく分類ごとの件数を読む。

| 分類 | 意味 | gate への影響 |
| --- | --- | --- |
| `entry_not_listed_count` | panel も as-of の価格を持たない | block しない。投資可能でなかった銘柄の除外は正しく、bias を生まない |
| `entry_price_gap_count` | panel は as-of の価格を持つのに forward が entry を持たない | block する。断面に数えた銘柄の forward 観測が無いので、population を無言で欠く |
| `unpriced_exit_count` | 窓中に系列が終わる（廃止 exit value なし） | block する。survivorship 露出そのもの |
| `future_horizon_count` | target が評価可能な最終取引日より先 | block する。cohort が満期に達していない |

authoritative な delisting exit value source が無い限り `unpriced_exit` は残るため、long-horizon result が blocked になるのは正しい。

`er_calibration` は価格収束成分 `er_reversion_annual` だけを price-only 実現値へ較正する。予測値は cohort 内の `er_reversion_annual` 中央値、実現値は同じ cohort の price return 中央値をそれぞれ引き、quintile ごとに median の相対値を比較する。`calibration_error` は `realized - predicted` である。配当と buyback の carry は price-only 実現値と同じ basis で観測できないため、この座標で絶対水準を較正しない。carry の妥当性は source と算出 contract を検証し、実現配当を備えた total-return dataset が利用できる場合に別の較正座標で扱う。

cache schema version は `2`。missing/mismatch/partial cache は `calibration-build --force` で再構築する。旧 reader は提供しない。

## Commands

```bash
uv run baibai-engine screening backfill-master --month-end-from 2022-09-01 --month-end-to 2026-06-30
uv run baibai-engine screening calibration-build --start 2023-01-01 --end 2026-04-30 --force
uv run baibai-engine screening calibration-evaluate --out .cache/calibration-eval.yaml
uv run baibai-engine screening calibration-evaluate \
  --run-purpose production_decision \
  --required-asof 2021-06-30 \
  --required-metric recommended_rank_top5 \
  --required-metric recommended_rank_top10 \
  --required-metric er_calibration
```

The retained diagnostics are selection top-5/top-10 median excess and trap rate, price-reversion E[r] predicted-versus-realized calibration, axis/gate/reversion regression diagnostics, and cohort coverage/integrity. They do not establish a track record or statistical significance.
