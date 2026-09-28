# 信用取引残高 公表移行初回検証（2026-09-25 残高）

価値tier: T1 — 公表制度変更を欠損・語義汚染なく通過する

## 判定

- machine verdict: `pass`
- observed_at_utc: `2026-09-28T12:41:09.997849+00:00`
- market store: `stores/market/market.sqlite`（2,062,225,408 bytes）
- market schema version: `28`
- 結論: 新旧 snapshot は事前固定した母集団・単位連続性 gate を通過した。これは同じ物理量の取込継続を確認するもので、日次軸の投資有効性は評価しない。

## Source snapshot

| series | balance date | rows | coverage fetched_at_utc | coverage key |
| --- | --- | ---: | --- | --- |
| legacy weekly | 2026-09-18 | 4,255 | 2026-09-25T13:03:23.678879+00:00 | `get_mkt_margin_interest:2026-09-18..2026-09-18` |
| all-issues daily | 2026-09-25 | 4,254 | 2026-09-28T12:40:59.124080+00:00 | `get_mkt_margin_interest:2026-09-25..2026-09-25` |

## 事前固定 gate

| gate | observed | requirement | result |
| --- | ---: | ---: | --- |
| `row_count_ratio` | 0.999765 | 0.98..1.02 | pass |
| `ticker_overlap_coefficient` | 0.999765 | >=0.98 | pass |
| `issue_type_agreement` | 0.999295 | >=0.95 | pass |
| `long_vol_total_ratio` | 1.001566 | 0.50..2.00 | pass |
| `short_vol_total_ratio` | 1.131355 | 0.50..2.00 | pass |

## 母集団

- overlap: 4,253
- added: 1 — `634A`
- removed: 2 — `1948`, `9914`

## 残高規模と銘柄別変化分布

銘柄別比率は旧値が正の overlap 銘柄だけで計算する。total ratio の gate は `long_vol` / `short_vol` のみに適用し、内訳は診断として記録する。

| field | legacy total | daily total | total ratio | comparable | ticker ratio q10 | median | q90 | 0→positive | positive→0 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `long_vol` | 3,495,442,263 | 3,500,916,141 | 1.001566 | 4,218 | 0.928571 | 1.000000 | 1.086410 | 6 | 7 |
| `short_vol` | 481,765,088 | 545,047,102 | 1.131355 | 2,576 | 0.813158 | 1.012179 | 1.718957 | 48 | 44 |
| `long_std_vol` | 1,636,893,736 | 1,645,417,071 | 1.005207 | 4,175 | 0.886448 | 1.000000 | 1.142567 | 8 | 6 |
| `long_neg_vol` | 1,858,548,527 | 1,855,499,070 | 0.998359 | 4,192 | 0.938909 | 1.000000 | 1.077141 | 3 | 11 |
| `short_std_vol` | 290,319,121 | 308,411,556 | 1.062319 | 2,412 | 0.792020 | 1.024047 | 1.944075 | 11 | 12 |
| `short_neg_vol` | 191,445,967 | 236,635,546 | 1.236044 | 1,694 | 0.857143 | 1.000000 | 1.721839 | 58 | 61 |

## 解釈境界

この検証は source coverage、件数、ticker 母集団、issue type、残高単位と内訳恒等式を確認する。
日次系列を既存 `margin_*`、gate、rank、E[r] へ接続する根拠にはしない。
