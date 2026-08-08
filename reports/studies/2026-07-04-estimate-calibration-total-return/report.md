# 較正計測の total return 化 — 配当 DPS 取り込み後の再計測（WU2 #293）

[baseline（price-only・2026-07-03）](../2026-07-03-estimate-calibration-baseline/report.md) の後続。配当（DPS）を cache に取り込み、超過リターンの一次基準を「配当 accrual 込み total return の母集団中央値対比」へ更新した再計測記録。**以後の計測・#295 の事前登録検証はこの total return 基準を正とする**。

**再現手順**:

```bash
uv run baibai-loop-screening calibration-build --start 2022-09-01 --end 2026-03-31 --force
uv run baibai-loop-screening calibration-evaluate --out .cache/calibration-eval-totalreturn-wu2.yaml
```

## 1. データ（refetch 結果と検算）

- 財務サマリー全窓 refetch（2021-08-02〜2026-07-03、3.2 時間）: 89,827 行（refetch 前 89,803 行 — 欠損なし・差分は期間中の新規開示）。DPS 実績（`DivAnn`）非 null 20,000 行（22% ≒ FY 開示比率 × 87% と整合）、予想（`FDivAnn`/`NxFDivAnn`）73,528 行（82%）。
- **spot check（AP-02）**: 2331 実績 29.2 円 / 予想 33.0 円（STAGE 2028 の性向 40–45% × EPS 予想と整合）。8929 実績 53 円 / 予想 58 円（thesis 記載の予想 58 円と一致・増配継続と整合）。**1911（1:3 分割銘柄）は分割前開示の実績 145 円が asof-basis 48.33 円 = 145÷3 と正確に一致**（分割正規化の実データ確認）。
- 母集団の dividend_yield coverage: cohort 中央値 100%（min 97%）。実績 DPS の「直近非 null 行からの carry-forward」で四半期断面でも欠損しない。

## 2. total return 化の効果（43 cohort・price-only → total return）

計算: 銘柄 total return = price return + entry 時点の実績配当利回り × 保有年数（accrual 近似）。超過 = 対母集団中央値（両辺 total return で整合）。

**主要軸の mean rank IC**:

| 軸 | 6m | 12m |
| --- | --- | --- |
| pbr | 0.234 → **0.250** | 0.296 → **0.317** |
| per_forward | 0.197 → 0.210 | 0.273 → 0.289 |
| dividend_yield（新規） | — → **0.225** | — → **0.290** |
| net_share_change_yoy | 0.136 → 0.143 | 0.177 → 0.185 |
| srp_pbr（自己レンジ） | −0.071 → −0.071 | −0.072 → −0.072 |

- 価値系軸は総じて IC が上昇（配当は割安・還元銘柄に偏るため、price-only はこれらの軸のリターンを系統的に過小評価していた）。自己レンジ percentile の負の予測力は不変。
- **dividend_yield 単体も強い軸に見えるが、注意**: (a) 高利回り ⊂ 割安・還元の regime 重複があり独立の情報量はこの表からは言えない、(b) H6（イールドチェイス）の懸念どおり decile 内トラップの検証は #295 の design/confirm で行う。この表を根拠に利回り追いをしない。

**selection replay（現行順の実力・total return 基準の新しい基準値）**:

| 順位系列 | 6m median excess | 6m 勝率 | 12m median excess | 12m 勝率 |
| --- | ---: | ---: | ---: | ---: |
| 本番推奨 top-5 | **−7.1%** | 25% | −3.2% | 44% |
| 素の割安順 top-10 | +2.4% | 62% | +2.5% | 56% |
| 素の割安順 top-20 | +3.0% | 60% | **+7.0%** | 79% |

現行推奨順の劣後は total return 基準でも同型（むしろ僅かに悪化 — 現行の先頭銘柄は配当 carry も薄い）。「割安ゾーン検出は機能・先頭の並べ替えが有害」という baseline の結論は不変。

## 3. 近似の限界（開示）

- 配当は accrual 近似（権利落ち月を特定しない）。個別銘柄の月次精度は持たないが、cross-section の比較には影響しない設計。増配・減配は entry 時点の実績利回りに固定される（保有中の変化は不算入 = 保守側）。
- benchmark 1306 は price-only のまま（ETF 分配金 ~2%/年を含まない参考値）。一次基準は母集団中央値対比であり影響しない。
