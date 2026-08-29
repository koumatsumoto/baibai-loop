# 予想 DPS spike guard の採用記録

価値tier: T1 — 一回性の特別配当を5年反復する収益として順位付けせず、持続的な割安候補へreview枠を渡す

## 結論

正のsplit-safe実績DPSがあるとき、実績の2倍を超える予想DPSは配当carryへ使わず、既存の実績DPS経路へ倒す。2026-08-28の実運用では、特別配当で上位へ出た3659と7595をreview setから除き、既存のprimary-research候補3836を残した。

長期比較には事前登録上の限界がある。Issue #1129は2倍guard、代替案、当日runの期待差分をhistorical replay前に固定したが、3y/5yの許容floorとdesign/confirm境界を固定していない。したがって以下の長期値を、閾値を選んだ盲検の採用試験とは呼ばない。外部review後、ownerは2026-08-30に5y top5 trap率の全期間+4.0ptを明示的に受容した。時間分割で前半+8.0pt、後半0.0ptだったことも同じaccepted riskに含め、結果を隠さず残す。

## 観察と仮説

- 3659は予想DPS 475円、実績45円で、予想の415円は一回性の特別配当だった。v19は配当利回り15.09%、E[r] 17.31%としてrank 1へ置いた。
- 7595も予想65円に20円の特別配当を含み、同じResearch Gateで反復可能性を人が再確認した。
- 同種の補正が2026-08-14、08-26、08-28に反復した。raw予想と実績を並べるannotationだけでは、review capを使う前の誤順位を防げない。
- 予想を常に実績以下へ制限すると2026-08-28のE[r]付き1,591件中1,535件を変える。配当carry全削除は持続的な配当の効果も失う。特別配当provider、schema、銘柄別例外は効果に対してsurfaceが大きい。
- そこで、正の実績があり予想がその2倍を超える31件（全3,705件の0.84%）だけを既存actual経路へ倒す仮説を採った。このうち現行のliquidity条件を通るのは23件である。実績0、未観測、split basis未解決ではforecastを維持する。

## 実運用比較

同じ2026-08-28 market snapshot、review cap 20、previous shortlist `shortlist-20260826-gate-writing-review`で比較した。

| ticker | v19 | v20 | review set |
| --- | --- | --- | --- |
| 3659 | E[r] 17.31%、rank 1 | E[r] 3.65%、actual basis | 除外 |
| 7595 | E[r] 9.10%、rank 3 | E[r] 6.32%、actual basis | 除外 |
| 3836 | E[r] 8.85%、rank 7 | E[r] 8.85%、rank 5 | 維持 |
| 5021 | rank外 | E[r] 7.48%、rank 19 | 流入 |
| 6436 | rank外 | E[r] 7.45%、rank 20 | 流入 |

v20 runはuniverse 3,705、liquidity後1,592、E[r]付き1,591だった。実装条件を満たすguard対象は全universeで31件、そのうちliquidity条件後は23件だった。rawの予想・実績とactual fallback basisはselection annotationに残る。

## 同一cohortの長期比較

baselineは`b039f878`のvaluation revision v19、variantはv20である。同じmarket storeから44 panel、823,500 forward rowsをそれぞれ作り、3yの44 cohortと5yの20 cohortを比較した。両方ともE[r] calibrationのMAE、top-bottom spread、positive shareは一致した。

| horizon / metric | median excess差 | win share差 | trap率差 |
| --- | ---: | ---: | ---: |
| 3y top5 | +3.0882pt | 0.00pt | -0.90pt |
| 3y top10 | +1.5742pt | +4.55pt | -0.91pt |
| 5y top5 | +1.3842pt | 0.00pt | **+4.00pt** |
| 5y top10 | +0.4609pt | -5.00pt | +0.44pt |

### 時間分割

全期間値を見た後の外部review指摘を受けた分割であり、盲検のconfirmではない。境界は結果を再探索せず、各horizonの暦順中央に固定した。3yは前半22 cohortを2021-09まで、後半22 cohortを2021-10以後、5yは前半10 cohortを2020-09まで、後半10 cohortを2020-10以後とした。

| horizon / window | top5 median差 | top5 trap差 | top10 median差 | top10 trap差 |
| --- | ---: | ---: | ---: | ---: |
| 3y 前半22 | -0.3387pt | +0.91pt | +0.4168pt | +0.46pt |
| 3y 後半22 | +6.5151pt | -2.73pt | +2.7315pt | -2.27pt |
| 5y 前半10 | +2.7683pt | **+8.00pt** | +0.9219pt | +2.01pt |
| 5y 後半10 | 0.0000pt | 0.00pt | 0.0000pt | -1.11pt |

5yのtrap悪化は前半へ集中し、後半ではtop5のmembership/結果が変わらなかった。これは価値低下を否定する証明ではない。明示的に受容した回帰幅であり、将来の満期cohortは既存の月次calibration更新で同じ指標を読み直す。専用monitor、閾値調整state、互換layerは作らない。

## Authority・coverage・再現

- v20 cache schema: `924be6323b32fc2f`
- v20 production rules hash: `599eab6b5e21593c`
- production decision: required 30 cohort、eligible 30、blocked/unresolved 0
- snapshot integrity: `ok`
- forward rows: 823,500、resolved 712,769、control-event exits 2,514、failure exits 789

```bash
uv run baibai-engine screening calibration-build \
  --start 2019-12-30 --end 2023-07-31 \
  --sqlite-path stores/market/market.sqlite \
  --calibration-dir <variant-dir> --force
uv run baibai-engine screening calibration-evaluate \
  --calibration-dir <variant-dir> \
  --run-purpose production_decision \
  --horizon 3y --horizon 5y \
  --required-asof 2020-03-31 --required-asof 2020-04-30 \
  --required-asof 2020-05-29 --required-asof 2020-07-31 \
  --required-asof 2020-08-31 --required-asof 2020-09-30 \
  --required-asof 2020-10-30 --required-asof 2020-11-30 \
  --required-asof 2020-12-30 --required-asof 2021-01-29 \
  --required-asof 2021-03-31 --required-asof 2021-04-30 \
  --required-asof 2021-05-31 --required-asof 2021-06-30 \
  --required-asof 2021-07-30 \
  --required-metric selection_rank_top5 \
  --required-metric selection_rank_top10 \
  --required-metric er_calibration \
  --required-metric er_level_calibration
```

## 採用境界

- 2倍は特別配当の事実認定ではない。持続的な還元転換も一時的にactual carryへ倒し得る。
- 倍率を外部設定、schema、warning、銘柄別listへ昇格しない。raw値を一次開示で再確認できる既存annotationだけを残す。
- 実績0・未観測・split basis未解決では、初配当や新規上場を消さないためforecastを使う。
- 2倍以下にも特別配当はあり得るので、Research Gateの一次開示確認は残す。
- 新しい満期cohortが悪化を示した場合も、このreportの数値を基準に自動調整しない。別のself-contained issueで再評価する。
