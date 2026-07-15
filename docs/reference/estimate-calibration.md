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

panel は cohort as-of 以下の最新 `eq_master` snapshot だけを読む。prior snapshot、snapshot unavailable、survivorship、delisting、corporate-action event coverage の不備は payload に残り、3y/5y evidence を block する。現行 provider に authoritative な membership、delisting、corporate-action event source はないため、実 cache の long-horizon result が blocked になるのは正しい。

forward row は `resolved` または明示的な unresolved status を持つ。target と entry はそれぞれ target/as-of 以下の最終取引日で解決し、15 日超の stale exit は resolved return に入れない。価格は as-of basis adjustment factor で正規化するが、metric basis は `price_return_only` であり配当 accrual を加えない。entry 時点の配当利回りを horizon 年数で按分する固定 accrual は、期間中の増配・減配・無配・支払時期を観測した実現配当ではないため、実現値として扱わない。

`er_calibration` は価格収束成分 `er_reversion_annual` だけを price-only 実現値へ較正する。予測値は cohort 内の `er_reversion_annual` 中央値、実現値は同じ cohort の price return 中央値をそれぞれ引き、quintile ごとに median の相対値を比較する。`calibration_error` は `realized - predicted` である。配当と buyback の carry は price-only 実現値と同じ basis で観測できないため、この座標で絶対水準を較正しない。carry の妥当性は source と算出 contract を検証し、実現配当を備えた total-return dataset が利用できる場合に別の較正座標で扱う。

cache schema version は `2`。missing/mismatch/partial cache は `calibration-build --force` で再構築する。旧 reader は提供しない。

## Commands

```bash
uv run baibai-loop-screening calibration-build --start 2023-01-01 --end 2026-04-30 --force
uv run baibai-loop-screening calibration-evaluate --out .cache/calibration-eval.yaml
uv run baibai-loop-screening calibration-evaluate \
  --run-purpose production_decision \
  --required-asof 2021-06-30 \
  --required-metric recommended_rank_top5 \
  --required-metric recommended_rank_top10 \
  --required-metric er_calibration
```

The retained diagnostics are selection top-5/top-10 median excess and trap rate, price-reversion E[r] predicted-versus-realized calibration, axis/gate/reversion regression diagnostics, and cohort coverage/integrity. They do not establish a track record or statistical significance.
