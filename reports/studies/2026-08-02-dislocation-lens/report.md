---
title: "急落ディスロケーション lens の計測結果"
summary: "低PER帯の急落quality群は1yで慢性割安群に劣後し、財務品質条件もtrapを一貫して判別しなかったため機械lensを不採用とした。"
doc_type: report
status: active
date: 2026-08-02
---

# 急落ディスロケーション lens の計測結果

価値tier: T1 — 一時的な売られすぎを慢性割安・財務毀損を伴う急落から分け、一次 research の着手順位を改善する。

## 結論

**判定は `negative`。急落ディスロケーションを candidate annotation、OP3文脈、gate、ranking、E[r]、FV、UIへ接続しない。検出は research / macro の人間工程に置く。**

主検定の1yでは、急落×quality passのA群が非急落×quality passのB群に対し、designで median excess −1.45pt・trap +4.22pt、confirmで−2.95pt・+7.55ptだった。両窓とも事前登録した median +3pt と trap改善を逆方向に外した。volatility、規模、業種、PERの全controlでも、両窓のtrap deltaがすべて正だった。

quality failのC群もA群より一貫して悪くならなかった。1y designのC−A trap deltaは−7.08ptで仮説と逆、confirmは+1.42ptだが片窓だけである。3y designのauthority eligible 4 cohortではA−Bが良い方向だったが、1yの独立2窓で再現せず、3y confirmはauthority eligible 0/2かつCが3件だった。良い3y designだけを採らない。

## 1. 事前登録と実行契約

定義、出力列、時間窓、control、採否条件は forward outcome参照前の commit `630490e5c5887ba9f7ec21a9ac18c6f810a10ea4` と [`2026-08-02-dislocation-lens-preregistration.md`](.../2026-08-02-dislocation-lens/preregistration/report.md) で固定した。評価実装は結果参照前の commit `3b49eff217463382abf78737d43ebfbe6bd7a421` で固定した。

実行コマンド:

```bash
.venv/bin/baibai-engine screening calibration-evaluate \
  --horizon 1y --horizon 3y \
  --out /tmp/dislocation-lens-eval.yaml
```

- cache schema: `9`
- panel: 80 cohort
- metric: cohortのresolved流動性母集団中央値に対する `price_return_only` excess
- trap: `excess < -0.20`
- 全期間evaluation SHA-256: `1219f37ea510c1447bd38ae6e25a14aff289c54de9885b040688363f8ba1ae73`
- 1y design / confirm SHA-256: `a44c5d21f57134a11fd25952bd6159230f88e353bf6692debcccd1836b94541e` / `47e51c12f181b55ec5ec8cb63728c462f03e55bc69aea8d76e01ce288529372e`
- 3y design / confirm SHA-256: `926716acbdfe9a4e85c171ffb71c0ed397084b12a3551a3c1502c9e07096a46a` / `d714b2b9851dc05c9596500c4df299ef20e519e81c4f53e95f5f260c1ce2eae2`

低 valuation は正の `per_trailing` 下位20%。急落は `price_change_60d <= -0.20`。quality passは `quality_cfo_positive` と `quality_no_dilution` がともにtrue、quality failは両方観測済みで少なくとも一方がfalseとした。欠損は補完していない。

## 2. 主検定

1yは全対象cohortのresolved metricを使う。3yは長期authorityのgeneric integrity条件を通るcohortだけを採否表へ使う。

| window | cohort | A / B / C n | A−B comparable | A−B median delta | positive share | A−B trap delta | C−A trap delta | 判定 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1y design | 38 | 142 / 2,660 / 86 | 26 | **−1.45pt** | 38.46% | **+4.22pt** | **−7.08pt** | negative |
| 1y confirm | 18 | 73 / 1,589 / 32 | 16 | **−2.95pt** | 50.00% | **+7.55pt** | +1.42pt | negative |
| 3y design authority eligible | 4 / 7 | 24 / 272 / 12 | 3 | +55.46pt | 100.00% | −9.27pt | +11.90pt | pass |
| 3y confirm authority eligible | 0 / 2 | 0 / 0 / 0 | 0 | — | — | — | — | insufficient |

3y designで除外した3 cohortの理由は、`entry_price_gap`、`priced_master_without_universe_flips_direction` + `unpriced_exit_flips_direction`、`priced_master_without_universe_return_unresolved` が各1件。3y confirmの2 cohortはともに `priced_master_without_universe_return_unresolved` だった。diagnostic値ではA/B/Cが7/206/3、A−B median −34.17pt、trap +4.21ptだが、採否には使わない。

## 3. 交絡確認

途中で不支持になっても止めず、事前登録順の4 controlを両1y窓ですべて計算した。値は急落quality−慢性割安qualityである。

| control | design median / trap delta | confirm median / trap delta |
| --- | ---: | ---: |
| `realized_volatility_60d` | +0.77pt / **+3.64pt** | +0.24pt / **+2.99pt** |
| `market_cap_oku` | +3.64pt / **+2.11pt** | +4.95pt / **+5.86pt** |
| `sector_33` | +6.75pt / **+5.60pt** | +14.05pt / **+9.28pt** |
| `per_trailing` | **−3.10pt** / **+7.99pt** | +1.97pt / **+3.82pt** |

medianだけは一部controlで正になったが、trapは8組すべて悪化した。必須のvolatility controlでもtrap悪化が両窓に残り、単なる高volatility反発を除いたあとに安定した下値保護は得られない。

## 4. 検算

- fixtureで急落境界 `-0.20`、A/B/C、quality欠損除外、低PER 20%境界、4 control、aggregateを固定した。
- `2024-05-31` の1y cohortをpanel / forward CSVから独立再計算した。流動性母集団1,429件、population median return −1.1381%、低PER帯252件。A/B/Cは13/152/2、median excessは−1.3824% / −3.6590% / −22.8050%、trap件数は4/31/1だった。A−B median +2.2766pt・trap +10.3745pt、C−A median −21.4227pt・trap +19.2308ptでevaluation YAMLと丸め前まで一致した。
- 対象unit test 43件、Ruff、対象mypyを計測前に通した。最終treeは全repository gateを別途通す。

## 5. 判定表

| 事前登録条件 | 観測 | 判定 |
| --- | --- | --- |
| 1y両窓 A−B median >= +3pt、positive share > 0.5、trap < 0 | design −1.45pt / 38.46% / +4.22pt、confirm −2.95pt / 50.00% / +7.55pt | fail |
| 1y両窓 C−A trap > 0 | design −7.08pt、confirm +1.42pt | fail |
| 3y両窓 H1/H2同方向 | design eligibleは通過、confirm authority 0/2 | insufficient |
| 1y両窓の全4 controlで median > 0、trap <= 0 | medianは一部負、trapは8組すべて正 | fail |
| 標本・authority | 1yは充足、3y confirmはCが3件かつauthority 0/2 | partial |

標本とmetricが揃う1y design / confirmの主仮説が両方とも逆方向なので総合判定は `negative`。3y confirm不足だけを理由に `insufficient` へ弱めない。

## 6. 運用上の結論

- 「60日で20%以上下落し、営業CF黒字・希薄化なしなら過剰反応候補」という機械注記は使わない。
- `price_change_60d` は観測情報のまま維持し、急落理由は一次IR、決算、corporate action、macro / 需給文脈を人間工程で確認する。
- 同じデータで閾値、quality成分、割安帯を変えて救済しない。決算起因 / 非決算起因を将来測る場合は、履歴coverageを確認して独立事前登録する。
- negative仮説の専用evaluatorを通常calibrationへ常設する価値はない。結果と再現用commitを残し、production treeには追加しない。
