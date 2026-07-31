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

3y/5y の blocker は 1 観測につき 1 つだけ立てる。ある判定から導ける別の判定を並べると、同じ欠けを二重に数えて理由の内訳が読めなくなるためである。

| 観測 | 測るもの | blocker が立たない条件 |
| --- | --- | --- |
| `master_snapshot_status` | population が cohort 日の断面から来ているか | `exact_date` |
| `survivorship_coverage_status` | panel の population が as-of の投資可能 universe を再現しているか | `asof_population_mismatch_count == 0` |
| `priced_master_without_universe_count` | as-of に価格が付き master にも在る銘柄を panel が評価できたか | `0` |
| `adjustment_factor_coverage` | 価格系列に分割調整 factor が揃っているか | `complete` |
| `delisting_exclusion.direction_stable` | 窓中に価格が途切れた銘柄の除外が結論を作っていないか | `true` |
| `entry_price_gap_count` / `future_horizon_count` / `unclassified_unresolved_count` | 未解決 row の分類（下記） | `0` |
| `input_range_clamped` / `candidate_partition_complete` | 入力窓が要求長を満たし、panel と forward の銘柄集合が一致するか | clamp なし / 一致 |

survivorship は population の性質なので panel が件数を測り、verdict は読み手が件数から導く（凍結すると complete の定義を変えたときに既存 cohort へ届かない）。bar store は市場から消えた銘柄の価格も保持するため、**as-of 当日に価格が付いた集合**を master snapshot と独立に観測できる。master が as-of より後なら当時上場していて現在は廃止された銘柄を欠き、前なら以降に上場した銘柄を欠く。どちらも断面が as-of の投資可能 universe ではないので incomplete とする。当日を基準にするのは、as-of 前に最終売買を終えた銘柄を master が持たないのは正しいからで、entry の staleness 許容（15 日）をここへ流用するとどの master でも mismatch を 0 にできなくなる。計測前に書かれた panel は件数を null として報告する（未計測を「欠けなし」と読めないようにする）。

`adjustment_factor_coverage` は bar store が答えられる唯一の corporate-action 観測である。系列を調整しない action（合併の対価、株主割当増資）はローカルに source が無いので、この残余は判定に畳まず、外部 source を要する既知の限界として扱う。

`unpriced_exit` / `adjustment_factor` の verdict は、survivorship が complete な断面でのみ意味を持つ。population から既に落ちている銘柄については系列終了も factor 欠落も観測され得ないので、survivorship が incomplete な cohort でこの 2 つが `complete` に見えるのは「濾された後の集合が綺麗」という意味にすぎない。

### 未解決 row の分類

未解決 row は 1 種類の欠陥ではないので、authority gate は総数ではなく分類ごとの件数を読む。

| 分類 | 意味 | gate への影響 |
| --- | --- | --- |
| `entry_not_listed_count` | panel も as-of の価格を持たない | block しない。production screen も同じ銘柄を universe から落とすので、較正の母集団は screen が選び得た集合と一致する |
| `entry_price_gap_count` | panel は as-of の価格を持つのに forward が entry を持たない | block する。断面に数えた銘柄の forward 観測が無いので、population を無言で欠く |
| `unpriced_exit_count` | 窓中に系列が終わる（廃止 exit value なし） | 件数では block しない。`delisting_exclusion` が結論の頑健性で判定する（下記） |
| `future_horizon_count` | target が評価可能な最終取引日より先 | block する。cohort が満期に達していない |
| `unclassified_unresolved_count` | 上のどれにも入らない未解決 status | block する。分類は allowlist なので、status が増えた日に無音で通らないための残余 |

`entry_not_listed` が非 block なのは「市場に無かった」に限らないので、panel が price を持ちながら universe へ入れられなかった銘柄は `priced_master_without_universe_count` として別に数え、こちらは block する。universe の除外条件が増えても、その分が非 block の側へ黙って流れ込まない。

entry は as-of の 15 日前までの close で解決するので、保有期間は名目 horizon より最大でその分長い。この許容が効く範囲まで bar の読み込み窓を広げてあり、`adjustment_factor_coverage` を判定する bar 集合も同じ窓に従う。

### 廃止銘柄の除外（`delisting_exclusion`）

authoritative な delisting exit value source が無い限り、窓中に系列が終わる銘柄は exit value を持たないまま cohort から落ちる。満期済み cohort は例外なくこれを含むので、件数で block すると 3y/5y の evidence は原理的に成立しない。代わりに、その除外が結論を作ったかどうかを cohort ごとに判定する。

除外された銘柄へ範囲の両端を代入して結論を再計算し、**cohort が報告した値と両方の代入とで向きが一致するときだけ** `direction_stable` を立てる。

| 代入 | 値 |
| --- | --- |
| 全損 | `price_return = -1.0` |
| 中立 | 同 cohort の resolved 銘柄の中央値 |

報告値を比較に含めるのは、それが authority gate の読む値そのものだからである。両方の代入で向きが揃っても報告値だけが逆を向くなら、その結論は除外が作ったものになる。向きは `recommended_rank_top5` / `recommended_rank_top10` が group の `median_excess` の符号、`er_calibration` が最上位 quintile の `median_realized_price_excess` − 最下位 quintile の同値の符号で定める。いずれかの場合で値が算出できず他の場合で算出できるときも、除外が「cohort が何か言えるかどうか」を決めているので不安定として扱う。

この判定は結論を下へ引く可能性に対しての bracket である。買収による廃止はプレミアム付きで中立代入の上に出るため、上側は挟まない。exit value そのものを外部 source から取る道は別に残る。

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
