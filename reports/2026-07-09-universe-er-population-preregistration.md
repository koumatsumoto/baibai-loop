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

1. `docs/workflow/screening.md` / `docs/reference/estimate-calibration.md` / `docs/operations/monthly-cycle.md` / `ai-value-bargain-selection` skill が、screen を注記、E[r] を ranking とする現在形に揃っている。
2. candidates schema / validator は `evidence_hits: []` を正しい fact として許容する。
3. PR では #309 を close し、検証表・金融 sector 診断・現 asof select 差分を self-contained に記載する。
