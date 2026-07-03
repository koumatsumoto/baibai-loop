# 長期見積り較正 — baseline 計測（2022-09〜2026-03 / 43 cohort）

初回の較正リプレイ計測記録。現行スクリーニングの軸・ランキングの長期予測力（3m/6m/12m）を、改訂前の比較基準（baseline）として固定する。実装仕様は [`../docs/reference/estimate-calibration.md`](../docs/reference/estimate-calibration.md)、計画は issue #291 / #292。

**再現手順**:

```bash
uv run baibai-loop-screening calibration-build --start 2022-09-01 --end 2026-03-31
uv run baibai-loop-screening calibration-evaluate --out .cache/calibration-eval-baseline.yaml
```

## 1. データと方法

- **cohort**: 月末営業日 43 個（2022-09-30〜2026-03-31）。J-Quants 履歴 backfill 済み cache（bars/fin とも 2021-08-02〜2026-07-01。プラン 5 年窓は 2026-07-03 の実取得テストで確認: 2021-09 取得成功・2020-09 は subscription 拒否）。
- **リプレイ**: 本番と同一実装（build_metrics / evaluate_screening / candidate_entry / build_selection_payload）。入力中立化 = macro_context なし・prior_research なし・previous_candidates なし・JPX flag なし。
- **forward return**: asof-basis close（adjustment_factor 累積）の price-only リターン。配当は未計上（WU2 #293 で total return 化）。**超過リターン = 銘柄リターン − 流動性母集団中央値**（同一 cohort の横断比較なので、配当の欠落と市場全体の方向は概ね相殺される）。
- **判定の規律**: 有意性は主張しない（6m 窓は月次 cohort 間で 5/6 重複）。効果量（median excess）と cohort 勝率で報告する。**本レポートは観察の記録であり、ランキング・閾値の変更はここから直接行わない**。変更は #291 §5 で本計測前に事前登録済みの仮説 H1–H8 を、WU4（#295）で design/confirm 時間分割により検証してからに限る。

## 2. Coverage と開示（結論より先に読む）

- 母集団（流動性通過 × forward 解決）: cohort あたり 1,077〜1,570（中央値 1,325）。
- **PBR 軸は母集団の 29%（中央値 378 銘柄）でしか測れていない**。bps が最新開示行のみから取られ、四半期行（bps 非掲載 約 8 割）が最新になる断面で大量欠損するため（FY 開示直後の cohort だけ n≈1,000）。PBR の数値は「bps を開示した直近 FY 型銘柄」に偏った部分標本での計測であり、carry-forward 補完（WU3）後に再計測する。per_trailing は中央値 1,111 で概ね全域。
- **survivorship**: universe は現在の master 断面のため、期間中に上場廃止した銘柄が母集団から漏れる（bars に存在し master に無い ticker は全 cohort で約 380）。廃止は TOB プレミアム付きが多く、割安側の計測にはおおむね保守的方向。
- **レジーム文脈**: 計測期間は日本株の強いバリュー期（6m 母集団中央値リターンは中央値 +4.7%・40 cohort 中 31 で正、12m は中央値 +10.4%）。「絶対的な安さが効く」という結果はこのレジームのベータを含み得る。横断（対中央値）比較で市場方向は中和しているが、バリューファクター自体の追い風は中和されない。
- 初期 cohort（2022-09〜2023-04 頃）は TTM coverage が薄い（per_trailing 670〜/母集団）。軸別 n を各 cohort に記録済み。stale price（廃止・長期停止で exit 価格が 15 日超古い）は cohort 中央値 0・最大 18 で無視できる規模。

## 3. 軸別の長期予測力（主要結果）

mean rank IC（cohort 平均）/ IC 正の cohort 率 / best decile（最割安 10 分位）の median excess 平均 / トラップ率（超過 < −20%）:

| 軸 | 6m IC | 6m IC+ | 6m D10 | 12m IC | 12m IC+ | 12m D10 | 12m trap |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **pbr（絶対）** | **0.234** | 90% | **+6.7%** | **0.296** | **100%** | **+15.4%** | 8.2% |
| per_forward | 0.197 | 90% | +3.5% | 0.273 | 100% | +10.0% | 13.7% |
| per_trailing | 0.183 | 88% | +3.2% | 0.254 | 100% | +7.9% | 16.0% |
| p_s | 0.163 | 90% | +2.7% | 0.226 | 100% | +5.9% | 12.6% |
| pcfr | 0.157 | 88% | +2.7% | 0.204 | 97% | +6.5% | 15.2% |
| net_share_change_yoy（自社株買い） | 0.136 | 88% | +2.9% | 0.177 | 100% | +4.9% | **10.8%** |
| cash_to_market_cap | 0.118 | 93% | +4.1% | 0.174 | 100% | +10.9% | 16.5% |
| ocf_yield | 0.117 | 85% | +2.9% | 0.148 | 94% | +6.4% | 14.9% |
| smg_pbr（sector 相対） | 0.164 | 97% | +1.8% | 0.209 | 100% | +4.8% | 14.8% |
| smg_per_trailing | 0.120 | 88% | +1.4% | 0.179 | 100% | +5.0% | 17.4% |
| gap_from_52w_low | 0.020 | 57% | −1.3% | 0.059 | 79% | −2.0% | 21.3% |
| price_change_60d（60 日下落） | −0.021 | 47% | −5.4% | 0.012 | 56% | −6.6% | **34.3%** |
| accruals_to_assets | −0.025 | 38% | −2.9% | −0.052 | 15% | −7.1% | 30.4% |
| **srp_pbr（自己レンジ percentile）** | **−0.071** | 33% | **−4.4%** | **−0.072** | 26% | **−6.7%** | **33.2%** |
| srp_per_trailing | −0.081 | 25% | −2.4% | −0.087 | 18% | −4.3% | 28.3% |
| equity_ratio | −0.073 | 12% | −3.7% | −0.111 | 6% | −8.3% | 33.1% |

観察（結論は WU4 の検証後）:

1. **絶対倍率の安さ（PBR > forward PER > trailing PER > P/S）が最も強く、horizon が長いほど強い**（12m で PBR IC 0.296・IC 正 34/34 cohort）。ただし PBR は §2 の部分標本注意。
2. **sector 相対 gap（smg_\*）は絶対倍率より一貫して弱い**。相対で安い銘柄より、絶対に安い銘柄が回復している。
3. **自己レンジ percentile（srp_\*）は負の予測力**。「自分の 3 年レンジの底にいる」はトラップ率が約 3 倍（33% vs PBR D10 の 8%）。現行 valuation-reversion の condition A の主材料と、60 日下落条件（condition B、price_change_60d 行）がともに逆効果方向 — 事前登録 H7 と整合する観察。
4. **自社株買い（net_share_change_yoy 縮小）は正の予測力 × 最低クラスのトラップ率** — H5 と整合。
5. equity_ratio 単独は負（資本効率の低さを拾う）。durability は「ランキング軸」ではなく「ゲート」に留めるべきという裏付け。accruals は負〜無で、単独軸としては使えない。

## 4. 現行 select 順の実力（headline baseline）

| 順位系列 | 平均 N | 6m median excess | 6m 勝率 | 6m trap | 12m median excess | 12m trap |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **本番推奨順 top-5**（playbook 固定順 + diversity） | 5.0 | **−6.8%** | **25%** | 26.0% | −3.2% | 26.5% |
| 本番推奨順 top-10 | 8.0 | −4.1% | 28% | 21.9% | −1.1% | 25.7% |
| 素の割安順 top-5（diversity なし） | 5.0 | −3.0% | 40% | 14.5% | −0.3% | 24.7% |
| 素の割安順 top-10 | 10.0 | +2.3% | 65% | 11.3% | +2.3% | 21.8% |
| 素の割安順 top-20 | 20.0 | +2.8% | 57% | 9.0% | **+6.4%** | 15.7% |

- **現行の本番推奨 top-5 は母集団中央値を大きく下回る**（6m median −6.8%・勝率 25%・トラップ率 26%）。一方、同じ screen 通過銘柄でも **順位を深く取るほど成績が改善し top-20 は正**になる。つまり通過集合には価値があるが、**先頭に並べる順序（playbook 固定順 = cash-rich 最優先、および強度キー）が有害**という観察 — H3 と整合する。
- 本番推奨は diversity cap（playbook あたり 2 × 4 playbook）で **8 銘柄で飽和**する（top-10 と top-20 が同値なのはこのため）。
- 例（2024-06-28 cohort の本番推奨 top-5）: cash-rich 先頭 2 銘柄が −12.1% / −37.6%、3–4 位の valuation-reversion が +5.0% / +11.8%、5 位 cashflow-yield −15.0%。
- PBR 最安 decile（+6.7%/6m・トラップ 4.5%）との差は 6m で約 13pt。**「割安ゾーンの検出」はできているのに「どれが一番お買い得か」の並べ替えで価値を失っている**のが現行基盤の最大の欠陥、という baseline になる。

## 5. Gate と収束の座標（WU3/WU4 の較正入力）

- **deterioration gate（営業利益 YoY ≤ −30% を除外）**: 割安 decile 内で gate 通過群は非通過群より 6m median excess が per_trailing +1.7pt / pbr +2.8pt 高い（cohort 勝率 55%）が、ocf_yield では −0.8pt（勝率 45%）と無効。H4 は「軸によっては弱く正」の観察。
- **収束実現（smg_pbr の implied upside 分位、6m）**: 実現は単調でない。upside ~50%（Q3）が最も実現し（+2.7%、gap の 5.3%/6m）、**upside 190%（Q5・deep discount）は 0.9% しか実現しない**。E[r] の reversion 成分は「深い割安ほど大きい」線形ではなく、実現率で減衰させる必要がある（WU3 の収束年数較正の直接入力）。

## 6. 検算（AP-02）

2024-06-28 cohort を評価 YAML と独立に CSV から再計算し一致を確認:
母集団 1,349・中央値リターン −2.86%（YAML −0.028571 ✓）／ PBR 軸 n=1,037・最安 decile median excess −4.67%（YAML −0.046704 ✓）／ 本番推奨 top-5 median excess −12.05%（YAML −0.120513 ✓）。分割正規化は 1:2 分割で asof-basis リターンが +20% になる unit test（`tests/test_calibration_grid_forward.py`）で固定。

## 7. 次工程への接続

- **WU2（#293）**: 配当込み total return 化（一次基準は両辺 total return で整合）。
- **WU3（#294）**: E[r] は「絶対倍率 anchor 主軸 + 実現率で減衰する reversion + carry（配当 + 自社株買い）」の形を較正する。bps 等の carry-forward 補完で PBR coverage 29% → 全域化し、PBR 軸を再計測する。
- **WU4（#295）**: H1–H8 の design/confirm 検証。**採否基準はこの baseline の分布を見て検証前に数値で固定する**。本レポートの観察（推奨順の劣後・srp の負 IC 等）は仮説の優先順位付けにのみ使い、変更の根拠には confirm 通過を要求する。
