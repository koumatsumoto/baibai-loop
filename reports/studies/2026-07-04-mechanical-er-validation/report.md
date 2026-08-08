# 機械 E[r]・BS carry-forward の検証（WU3 #294）

[total return 版計測（2026-07-04）](../2026-07-04-estimate-calibration-total-return/report.md) を基準に、WU3 で導入した (a) BS 系 fact の carry-forward、(b) 機械 E[r]（成分分解付き年率見積り）の効果を較正リプレイ（43 cohort・total return 基準）で検証した記録。

**再現手順**:

```bash
uv run baibai-loop-screening calibration-build --start 2022-09-01 --end 2026-03-31 --force
uv run baibai-loop-screening calibration-evaluate --out .cache/calibration-eval-wu3-final.yaml
```

## 1. BS carry-forward の効果（導入ゲート: coverage 改善 × IC 非劣化）

| 指標 | 導入前 | 導入後 |
| --- | ---: | ---: |
| PBR 軸の標本（cohort 中央値 / 母集団 1,324） | 378（29%） | **1,318（99.5%）** |
| PBR mean IC 6m / 12m | 0.250 / 0.317 | **0.257 / 0.337** |
| PBR best decile median excess 6m / 12m | +7.0% / +16.0% | **+8.9% / +21.3%** |
| cash_to_market_cap IC 6m / 12m | 0.126 / 0.183 | 0.161 / 0.225 |

- 最強軸だった PBR が「FY 開示直後の銘柄だけの部分標本」から**母集団ほぼ全域の計測**になり、IC・decile とも改善（部分標本バイアスはむしろ予測力を弱めていた）。cash_eq の carry-forward も同様に cash 系軸を改善。
- per_trailing / equity_ratio は不変（TTM 合成・四半期開示に載る field は carry-forward の影響を受けない = 想定どおりで、変更の副作用がないことの確認）。
- staleness は `bs_carry_forward_fields` / `bs_carry_forward_lag_days` として candidates に事実記録される。

## 2. 機械 E[r] の予測力（実現率 0.10）

| 軸 | IC 6m | IC+ 6m | D10 6m | trap 6m | IC 12m | IC+ 12m | D10 12m |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **er_annual（blend）** | 0.191 | 97.5% | +3.7% | **5.8%** | 0.250 | **100%** | +7.6% |
| er_reversion_annual（reversion 単体） | 0.111 | 95% | −0.2% | 14.0% | 0.153 | 100% | +0.7% |
| pbr（参考・最強単体軸） | 0.257 | 90% | +8.9% | 4.7% | 0.337 | 100% | +21.3% |

- **blend（reversion + carry）は reversion 単体を大きく上回る**（IC・D10・trap すべて）。deep discount 単体はトラップを含み、carry（配当 + 自社株買い）が質フィルタとして機能している設計意図どおりの結果。
- 単体軸 PBR の IC は E[r] より高い。E[r] の価値は IC の最大化ではなく、(i) IC 正の一貫性（12m で 34/34 cohort）、(ii) 低トラップ率、(iii) **%/年の単位を持つ見積りとして thesis / calibration に接続できること**にある。ランキングへの反映は #295 の事前登録検証（H3: E[r] informed 順 vs playbook 固定順）で判定する。

## 3. E[r] calibration（予測 vs 実現・実現率パラメータの較正記録）

実現率の 2 点比較（grid search はしない。目的関数を先に固定した 1 回の較正）:

| 実現率 | 予測 vs 実現の水準 | er_annual IC 6m | best decile trap 6m |
| --- | --- | ---: | ---: |
| **0.10（採用）** | 実現スプレッド ≒ 予測 × 2.4（保守側に過小予測） | **0.191** | **5.8%** |
| 0.20 | 実現 ≒ 予測 × 1.5（近い） | 0.169 | 8.3% |

- 実現率を上げると水準は合うが、blend 内の reversion 重みが増えて deep-value 側に順位が寄り、**並べ替え品質が劣化する**。E[r] の目的（並べ替え情報 + 保守的アンカー）に照らし 0.10 を採用。
- **含意（読み方）**: E[r] の絶対値は保守的で、この計測窓（バリュー優位レジーム）では実現が予測を約 2.4 倍上回った。quintile は両 horizon で単調（0.10 版: 12m で Q1 予測 −2.6%→実現 −9.1%…Q5 予測 +2.6%→実現 +3.3%/6m、12m も同型）であり、**順序情報としては較正済み**。絶対値を thesis の期待利回りにそのまま使う場合は保守側バイアスを織り込む。
- レジーム注意: 2.4 倍はバリュー期の実現率であり、他レジームでの水準較正は cohort が別レジームを含むようになった時点で再計測する。

## 4. select への転記（運用確認）

select recommendations / ticker-profile に `er_annual`・成分（reversion/carry）・`er_anchor_metrics`・`fv_sector_median_yen`・`fv_self_range_yen`・`dividend_yield` が転記される。thesis の手動 FV は機械アンカーを出発点にし、乖離理由を本文に書く（workflow/research.md）。現 asof での実出力検算は §5。

## 5. 検算（AP-02）

- estimates 純関数: anchor blend（(0.25+0.50)/2 = 0.375）・cap・dilution clip を unit test で固定（`tests/engine/test_screening_estimates.py`）。
- 実データ: 2331 = 実績 DPS 29.2 円 / 予想 33.0 円（STAGE 2028 性向と整合）、8929 = 53/58 円（thesis 記載と一致）、1911 = 分割前 145 円 → 48.33 円（145÷3 と一致）。
- 較正評価は store --force 再構築（rules_hash provenance 検証通過）後の出力。パラメータ 2 点比較は同一 store 手順の独立 2 run。
