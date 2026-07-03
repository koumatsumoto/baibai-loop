# estimate-calibration — 長期見積り較正リプレイの実装仕様

見積り・ランキングの長期予測力（3m/6m/12m horizon）を過去データで機械計測する L2 の決定論的処理。思想上の位置づけ（正式な計測経路・誠実性の規律）は [`../doctrine.md`](../doctrine.md) 柱 5 が正本。本 doc は操作・入出力・計算仕様を持つ。

## 1. 目的と非目的

- **目的**: doctrine §2 の見積り較正の母数 (b)。過去の各 asof で機械見積り・screen 判定・select 順位を point-in-time に再構成し、その後に実現したリターンと突き合わせて「どの軸・どの順位付けが長期の割安回復を予測できているか」を数値化する。
- **非目的**: 短期（3 か月未満）horizon の screen 成績最適化・grid search・track record の提示（doctrine §8）。

## 2. 操作

```bash
# 1) 月次 panel + forward return を local store へ構築（cache のみ・provider 不要）
uv run baibai-loop-screening calibration-build --start 2023-01-01 --end 2026-04-30

# 2) 評価（rank IC / decile / selection replay / トラップ率 / gate / 収束）
uv run baibai-loop-screening calibration-evaluate --out .cache/calibration-eval.yaml
```

- panel / forward は `data/screening/calibration/` に CSV + meta YAML で永続化する（再生成可能な L2 中間物。git に積まない）。既存 panel は skip、`--force` で再構築。`calibration-evaluate` の YAML 出力も再生成可能な計測出力であり、CSV store とともに **安定契約 1（CLI YAML 出力）の対象外**（形の正本は本 doc §5）。
- **provenance ガード**: 各 panel の meta YAML は構築時の screening rules 内容 hash（`rules_hash`）を持ち、evaluate は全 cohort の一致と「同一月の重複 cohort が無いこと」を検証してから集計する（rules 改訂後の再構築漏れ・月中 cohort の混入による二重計上を機械検出する）。grid は「翌月の営業日が data に存在する完全月」だけを cohort にする。
- forward return は build のたびに再計算する（時間経過で新しく解決する horizon を取り込むため）。
- 月次グリッドは bars の実在（当日 cross-section ≥ 2,000 銘柄）から月末営業日を導出する。

## 3. リプレイの構成（本番との一致と中立化）

本番 run / select と同じ実装（`build_metrics` / `evaluate_screening` / `candidate_entry` / `build_selection_payload`）をそのまま呼び、順位ロジックを複製しない。候補行の組み立ては `screening/candidate_build.py` に単一化されている。

入力の中立化（リプレイ固有の差分はこれだけ）:

- `macro_context=None` — macro は診断 annotation で順位に使わないため。
- `prior_research_by_ticker={}`・`previous_candidates=None` — thesis record 由来の suppression は published_at ≤ asof の point-in-time チェックを持たず、現在の判断が過去 cohort に漏れるため遮断する。
- JPX 規制 flag は過去断面が cache に無いため空（規制除外は annotation 数銘柄規模）。

panel には全普通株（universe scope）を記録し、`in_population`（selection.liquidity 通過 = 流動性母集団）を評価の母集団 flag にする。`selection_rank` は diversity cap を実質無効化した本番順位付け（E[r] 主キー）そのままの順位、`recommended_rank` は本番 diversity cap 適用の推奨順位（深さ 50）。評価はこのほかに `er_ranked_topN`（pass_screen × er 非 null 集合の E[r] 降順・仮想 replay）と `er_population_topN`（screen gate なしの母集団選抜・gate の付加価値診断）を出力する。

## 4. forward return

- 価格は adjustment_factor 累積の asof-basis 正規化（`market/bars.py` の `asof_basis_closes`）。cache の `adjustment_close` は遡及調整が混在するため使わない。
- horizon は暦日 {3m: 91, 6m: 182, 12m: 365}。target は on-or-before の営業日に解決する。
- entry / exit とも直近 bar が 15 日超古い場合は取引実態なしとして unresolved / `stale_price` に計数する（上場廃止・長期停止を黙って落とさない・ゼロ扱いしない）。
- **total return は配当 accrual 近似**: 銘柄側は price return + entry 時点の実績配当利回り（`dividend_yield`）× 保有年数で近似する（権利落ち月は特定しない。横断比較が目的で、支払月 1–2 か月のずれは cross-section にほぼ影響しない）。`dividend_yield` 欠損は 0 扱いとし、cohort ごとの coverage を評価に開示する。benchmark（1306）は price-only のままの参考値（ETF 分配金 ~2%/年を含まない）。

## 5. 評価指標

| 指標 | 定義 | 駆動する決定 |
| --- | --- | --- |
| rank IC | 軸値と forward 超過リターンの Spearman 相関（cohort 別 + 平均 + 正の割合） | 軸の採否 |
| decile | 軸 10 分位の超過リターン分布。best decile の median / trap 率 / best−worst spread | 軸の強度・閾値の当否 |
| selection replay | `recommended_rank` / `selection_rank` top-5/10/20 の超過リターンと cohort 勝率 | ランキング改訂の二次確認 |
| トラップ率 | 超過リターン < −20% の比率 | ゲートの実効性 |
| gate 条件付き spread | 割安 decile 内の deterioration gate（YoY ≤ −30%）通過 / 非通過差 | ゲートの keep / 改訂 |
| 収束実現 | sector 中央値倍率までの implied upside 分位 × 実現超過 | E[r] 収束年数の較正 |

- **超過リターンの一次基準は流動性母集団の中央値**（選定スキルの直接計測。両辺が同じリターン定義で整合する）。TOPIX ETF（1306・price-only）は市況文脈の参考値。
- cohort は forward 窓が重複し独立でないため、有意性検定はせず効果量と cohort 勝率で判断する（doctrine 柱 5 の誠実性規律）。
- 軸ごとの最小標本 100 / IC 最小標本 30 を満たさない cohort × 軸は skip として現れる。

## 6. cohort の採用ゲートと開示

- 各 panel の meta YAML に universe / population / 指標 coverage / TTM exact 数 / survivorship（master 現在断面に無い bars 銘柄数）/ 実効読み出し窓（coverage 床へのクランプ有無）を記録する。
- 評価レポートは cohort ごとの coverage と除外理由を開示する。TTM coverage が薄い初期 cohort（fin cache 床直後）は主評価から除外し、除外を明記する。
- universe master は現在断面のみのため、上場廃止銘柄は母集団から漏れる（survivorship）。件数を開示し、方向（廃止は多くが TOB プレミアム付きで、割安側の計測にはむしろ保守的）を注記する。

## 7. データ窓

- J-Quants プランの取得窓は過去 5 年（2026-07-03 実取得テストで確認: bars/fin とも 2021-09 取得成功・2020-09 は subscription 拒否）。bars + fin summaries は取得可能な最古まで backfill 済みの cache を前提にする。
- 読み出し窓（bars 1200 日 / fin 730 日）が cache 床より前に出る asof では床にクランプし、実効窓を meta に記録する。

## 8. 参考

- [`../doctrine.md`](../doctrine.md) 柱 5 / §8: 計測経路の位置づけと誠実性規律
- [`../workflow/screening.md`](../workflow/screening.md): screen / select の運用
- [`./valuation-metrics.md`](./valuation-metrics.md): 軸になる valuation 指標の算出仕様
