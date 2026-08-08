# universe E[r] population ranking 事前登録

## 0. 目的と盲検性の限定

目的は #309 の universe 再設計を、改善ループの採否規律に従って本番実装へ移すことである。select の ranking 対象を「playbook screen 通過集合」から「selection liquidity を通過し、`er_annual` が非 null の母集団」へ広げ、`evidence_hits` は ranking gate ではなく割安型の注記として扱う。

盲検性の限定: `er_population_top10` が `er_ranked_top10` を design / confirm の 6m・12m で非劣化以上に上回る観察は #309 と `reports/2026-07-06-h7-gate-followup-validation.md` で既知である。このサイクルの新規性は、(a) 本番 `run` / `select` / calibration replay を同じ母集団契約へ更新すること、(b) 実装後の `recommended_rank` が既知の `er_population` 計測と一致すること、(c) 金融 sector を E[r] 母集団に含めてよいかを明示的に診断することにある。

## 1. 実装仮説

1. candidates YAML は全普通株の L2 fact を保持し、`evidence_hits[]` は該当した playbook screen の注記として空配列を許容する。
2. select は candidates YAML から selection liquidity を通過した銘柄を取り、`metrics.er_annual` が非 null の候補を E[r] 降順で ranking する。
3. `selection_playbook` / `selection_metrics` は evidence がある候補だけに付く注記であり、evidence がない候補の採用可否を直接制限しない。
4. calibration replay は本番 `run` / `select` と同じ候補契約を使い、過去 asof の population candidate を `build_selection_payload` に渡す。

## 2. 採用基準

実装後に本番 rules で calibration store を `--force` 再構築し、以下を満たす場合だけ採用する。

1. `recommended_rank_top10` の 6m mean median excess が、同じ store の `er_population_top10` に対し design / confirm の両窓で **±0.5pt 以内**。
2. `recommended_rank_top10` の 6m mean trap rate が、同じ store の `er_population_top10` に対し design / confirm の両窓で **±0.5pt 以内**。
3. 12m の `recommended_rank_top10` でも 1・2 と同方向で、乖離が **±1.0pt 以内**。
4. 現 asof の運用テストで、playbook evidence なし候補が E[r] 上位なら recommendation に入ること、かつ `selection_playbook: null` が注記として出ることを確認する。

## 3. 金融 sector の扱い

金融 sector は銀行業 / 証券・商品先物取引業 / 保険業 / その他金融業とする。これらは従来 playbook から除外されていたが、E[r] 母集団 ranking では `er_annual` が非 null なら候補になり得る。

採用判断は以下で固定する。

- 金融 subset の `er_annual` 6m mean rank IC が design / confirm の両方で負、または金融 subset の top decile trap rate が全母集団の `er_annual` best decile trap rateを両窓で +10pt 超悪化する場合、金融 sector は今回の ranking 母集団から除外する。
- 上記の棄却条件に当たらなければ金融 sector は含める。ただし、coverage・IC・trap を report に開示し、月次監視事項に残す。

## 4. 運用・docs の完了条件

1. `docs/workflow/screening.md` / `docs/reference/estimate-calibration.md` / `docs/operations/decision-cycle.md` / `ai-value-bargain-selection` skill が、screen を注記、E[r] を ranking とする現在形に揃っている。
2. candidates schema / validator は `evidence_hits: []` を正しい fact として許容する。
3. PR では #309 を close し、検証表・金融 sector 診断・現 asof select 差分を self-contained に記載する。

## 5. 採否結果

calibration store は `stores/screening/calibration/variants/universe-er-population` に `--force` 再構築した。評価範囲は design = 2022-09-01..2024-06-30、confirm = 2024-07-01..2026-06-30。selection の top10 は `recommended_rank` / `selection_rank` / `er_population` が同一になり、採用基準 1-3 を満たす。

| window | horizon | recommended mean median excess | er_population mean median excess | median delta | recommended trap | er_population trap | trap delta |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| design | 6m | 0.141130 | 0.141130 | 0.000pt | 0.0864 | 0.0864 | 0.000pt |
| design | 12m | 0.300890 | 0.300890 | 0.000pt | 0.1136 | 0.1136 | 0.000pt |
| confirm | 6m | 0.066871 | 0.066871 | 0.000pt | 0.0556 | 0.0556 | 0.000pt |
| confirm | 12m | 0.145813 | 0.145813 | 0.000pt | 0.0833 | 0.0833 | 0.000pt |

## 6. 金融 sector 診断

金融 sector は E[r] 母集団に含める。6m の金融 subset E[r] rank IC は design / confirm とも正で、金融 top decile trap は全母集団 E[r] best decile trap より低い。事前登録した除外条件（両窓で rank IC が負、または両窓で trap が +10pt 超悪化）には該当しない。

| window | cohorts | mean financial n | mean financial rank IC | IC positive share | financial top decile trap | all E[r] best decile trap | trap delta |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| design | 22 | 90.3 | 0.1196 | 0.8636 | 0.0202 | 0.0584 | -3.818pt |
| confirm | 18 | 104.3 | 0.0212 | 0.6667 | 0.0351 | 0.0578 | -2.271pt |

月次監視では金融 subset の 6m rank IC と trap delta を継続確認する。

## 7. 現 asof 運用テスト

2026-07-08 asof で `run` と `select` を実行した。`run` は candidates を 3,744 件生成し、TTM exactness / 業績悪化入力欠損の partial warning を返したが、選定 contract の確認に必要な candidates YAML は生成された。`select` の counts は input 3,744、liquidity 通過 1,475、E[r] 非 null 1,473、evidence annotation 459、E[r] 欠損 2。

上位 5 件のうち 3 件は `evidence_hits: []` / `selection_playbook: null` のまま recommendation に入り、採用基準 4 を満たす。

| rank | ticker | name | E[r] | evidence hits | selection_playbook |
| ---: | --- | --- | ---: | ---: | --- |
| 1 | [5445 東京鐵鋼](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A5445) | 東京鐵鋼 | 0.2035 | 0 | null |
| 2 | [9008 京王電鉄](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A9008) | 京王電鉄 | 0.1997 | 0 | null |
| 3 | [8219 青山商事](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A8219) | 青山商事 | 0.1827 | 0 | null |
| 4 | [4116 大日精化工業](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A4116) | 大日精化工業 | 0.1754 | 1 | cashflow-yield-discount |
| 5 | [4008 住友精化](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A4008) | 住友精化 | 0.1483 | 1 | cashflow-yield-discount |
