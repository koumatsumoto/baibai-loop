# Capacity-native universe — Stage 0 outcome-free feasibility

価値tier: T1 — 固定的な時価総額・平均売買代金floorの外側から、実際の注文金額で取得・退出できる候補を回収できるかを、実現returnを見る前に判定可能にする。

## 境界

この記録は `panel-*.csv` の point-in-time membership / E[r] と、各 cohort as-of 以下の `jquants_daily_bars` / 同日 master snapshot だけを読む。`forward-*.csv`、realized return、過去の outcome report は読まない。機械成果物 [`stage0.yaml`](./stage0.yaml) も `outcome_data_read: false`、`forward_inputs: []` を明示する。

閾値は Issue #884 の固定値をそのまま使い、候補件数を見て変更していない。

## 固定した計算契約

- 最低単元: cohort as-of の未調整終値 × 100株
- 60-session: 全市場で2,000銘柄以上のbarがある直近60日
- p20売買代金: 上記60日の nearest-rank 20%点。row欠損、volume欠損、turnover欠損は無出来として0円にする
- usable session: 未調整終値、調整後終値、volume、turnoverがすべて有限な非負値で、両終値が正
- nonzero volume share: volumeとturnoverがともに正のsession数 ÷ 60
- Amihud / zero-return: 連続する市場sessionの両方に調整後終値があるpairだけで計算し、membership gateには使わない
- primary membership: 30万円、1%参加率、5日以内、最低単元30万円以下、usable 57/60以上、nonzero volume 95%以上、listing span proxy 365日以上
- current policy: 同じ `pass_screen` / E[r] 母集団へ、時価総額100億円、20日平均売買代金1億円、listing span proxy 182日を適用する
- 両policyは historical panel の同じ ordinary-share / financial contractを共有する。historical JPX regulation flagはpanelに無いため、両方とも同じ空flag replay contractであり差分要因にはしない

`trading_unit = 100` は推測ではない。JPX は全国取引所の内国株式について2018-10-01に100株への統一が完了したと公表しており、最古cohortは2019-11-29である（[JPX「売買単位の統一」](https://www.jpx.co.jp/equities/improvements/unit/index.html)）。対象期間を覆す単元例外は確認されなかった。

## 結果

入力は現行 `rules_hash: f4a3f3f20838e1cd` の80 cohort（2019-11-29〜2026-06-30）、screened candidate 123,978観測である。market storeの最新日は2026-08-10。

| sufficiency | 実測 | floor / ceiling | 判定 |
| --- | ---: | ---: | --- |
| capacity fact coverage | 98.65% | 95%以上 | pass |
| capacity top-20 availability | 100.0% | 90%以上 | pass |
| capacity-only outside-current top-5 availability | 97.5% | 75%以上 | pass |
| 1y design unique / max ticker share | 84 / 5.16% | 25以上 / 15%以下 | pass |
| 1y holdout unique / max ticker share | 39 / 12.17% | 25以上 / 15%以下 | pass |
| 3y all unique / max ticker share | 82 / 5.29% | 15以上 / 15%以下 | pass |
| 3y post-COVID unique / max ticker share | 48 / 7.50% | 15以上 / 15%以下 | pass |
| 5y matured aggregate unique / max ticker share | 44 / 10.68% | 8以上 / 15%以下 | pass |

primary capacity eligible数は cohort 最小170、中央値786、最新886。capacity-onlyは延べ37,549観測・2,525銘柄で、時価総額中央値134億円、sector HHI 0.0924、carry優勢76.53%。参考診断の「グロース市場かつ40〜100億円」は6.57%で、gateには使わない。

capacity eligibleの最低単元中央値は111,500円、1%参加率で30万円を退出する日数中央値は0.73日、60-session zero-return share中央値は1.69%、no-trade share中央値は0%。current core top-20との重複中央値は50%で、capacity contractは十分に異なる比較集合を作る。

## Stage 0 verdict

`pass`

U2で windows、basis、cost、sufficiency、effect gate、4値verdict、cleanup、独立検算を固定してcommitするまで、forward outcomeは読まない。

Stage 0 artifact SHA-256: `5c16ff8a180de0ea8be10ebda89897c0f7202a851262d3630f8b14bc46e59206`
