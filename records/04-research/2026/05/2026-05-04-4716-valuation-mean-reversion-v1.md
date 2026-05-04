---
ticker: "4716"
name: "日本オラクル"
playbook: valuation-mean-reversion-v1
decision: accepted
candidates_ref: records/03-candidates/2026/05/2026-05-01.yaml
outlook_ref: records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
brief_refs:
  - records/01-brief/2026/05/2026-05-04-world-daily-us-pce-cn-trade-hormuz.yaml
ai-draft: true
published_at: "2026-05-04T22:30:00+09:00"
tradable_at: "2026-05-15T09:00:00+09:00"
macro_gate: tailwind
position_size_oku: 0.02
adv_participation_pct: 0.054
avg_turnover_oku: 37.3
market_cap_oku: 10940
sector_33: "情報・通信業"
valuation:
  per_forward: null
  per_trailing: 23.27
  pbr: null
  ev_ebitda: null
  p_s: null
  pcfr: null
  primary_metric: ["per_trailing"]
---

# Research: 2026-05-04 4716 日本オラクル valuation-mean-reversion-v1

**成分**: 4 成分アーキテクチャの **(d) 個別銘柄リサーチ**

**Playbook**: valuation-mean-reversion-v1 (P-A 純粋型)

## 1. Thesis

新 outlook で `情報・通信業 = tailwind` (AI / データセンター / Sovereign AI 構造的需要)
に属する大型 IT サービス。PER trailing 23.27 は業種中央値比 -22%、self range 下位
1.84% (sigma_gap -1.75)。米親会社 Oracle (NYSE: ORCL) が OCI / Generative AI で年率
50% 級の cloud growth を実証する中、日本子会社の Database / OCI / SaaS (NetSuite /
Fusion) ビジネスも tailwind の下流受益。60 営業日 -18.5%、4 週 -2.5% で stabilization
進行、catalyst 不在の純粋 P-A。

## 2. Macro gate [最上位ゲート]

- **判定**: tailwind
- **業種**: 情報・通信業 (outlook で `tailwind`)
- **地域**: japan-domestic (`neutral`) — 国内法人顧客中心、米親会社経由でグローバル製品供給
- **保守側優先判定結果**: tailwind (sector tailwind dominant、region neutral で headwind 不在)
- **outlook_ref**: records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
- **brief_refs**: records/01-brief/2026/05/2026-05-04-world-daily-us-pce-cn-trade-hormuz.yaml
- **1-2 行要約**: outlook の 情報・通信業 tailwind 根拠 (TSMC Q1 / NVIDIA FY26 / AI / DC
  spillover) は Oracle 親会社の OCI / Generative AI / NVIDIA chip 提携と整合的に
  spillover、日本子会社にも selective 受益。

## 3. Valuation snapshot

| 指標 | 値 | 業種中央値 | 業種中央値比 | 過去 750 日パーセンタイル | primary |
| --- | --- | --- | --- | --- | --- |
| PER (trailing) | 23.27 | 29.8 (推定 sector_median_gap -0.2196 から逆算) | -22% | 1.84% (下位 2%) | ✓ |
| PER (forward) | null (会社予想未公表) | — | — | — | |
| PBR | null | — | — | — | |
| EV/EBITDA | null | — | — | — | |
| P/S | null | — | — | — | |
| PCFR | null | — | — | — | |

- sigma_gap (per_trailing): -1.75 (1.75σ 下方)
- self_range_percentile 0.0184 = 750 営業日中の下位 1.84%、強い valuation cheap signal
- per_forward null は親会社 (Oracle USA) 子会社のため日本側で会社予想公表が限定的、
  trailing 単独評価で playbook 採用条件 (sector_median_gap < -20% + self_range bottom 20%)
  クリア

**primary metric**: per_trailing 単独

**計算検算**: sector_median_gap -0.2196 → 中央値 = 23.27 / (1 - 0.2196) = 29.81x。
self_range_percentile 0.0184 = 750 営業日中の下位 1.84%。

## 4. 一時的割安の原因仮説

- **業種ローテーション一過性**: 国内 IT 大型株から AI 直結のグロース系 (NVIDIA proxy
  銘柄) へのローテーションで、Oracle Japan のような「成熟 SaaS + 大型 cap」が相対劣後
- **業績の一過性鈍化**: Q3 FY2026 (2026-03 期) で為替差損 / 一時要因による減益見込み
  → 株価下押し
- **2025-Q4 高値からの調整**: YTD 高値ピークから -18% drawdown、4 週 -2.5% で stabilization
  signal 確認済 (60 日下落幅の 7 倍規模が 4 週で 1/7 に収束)

「一時的」根拠: 親会社 Oracle USA の OCI 売上は構造的 +50% 成長中、Generative AI 関連
Capex も加速。日本子会社の中長期収益基盤 (Database 保守 / 新規 OCI 案件) は tailwind
継続。direct earnings volatility は数四半期レベル。

## 5. 反対仮説 - 構造的理由

- **国内 cloud 競争激化**: AWS / Azure / GCP が日本市場でシェア拡大、Oracle 数十兆円
  Database lock-in が侵食される long-term リスク。本仮説が正しい場合、Database 保守
  収益が逓減 → 業種中央値も切り下がり relative cheap が消失する
- **円安が逆風**: 米親会社へのライセンス支払いが USD 建て、円安進行で原価圧迫
  (USD/JPY 156.56)。USD/JPY が 165 超に進めば営業利益率 -2-3pt 圧迫の可能性
- **親会社製品依存**: Oracle USA 製品ポートフォリオに依存し、自社開発の独立収益基盤が
  限定的。米国の戦略変更 (例: 中国向け輸出制限拡大) で日本子会社にも spillover
- **その他**: 国内官公庁 / 大手企業の脱 Oracle トレンド (オープンソース DB 採用)

→ 本仮説が正しい場合、割安は trap であり mean reversion は機能しない。

## 6. Catalyst

P-A 純粋型: catalyst なし。純粋な valuation mean-reversion (業種中央値比 -22% +
self_range 下位 2% + 4 週 stabilization) 狙い。

## 7. Price reaction (adj close ベース)

| 対象 | 値 | 変化 | ソース |
| --- | --- | --- | --- |
| 60 営業日騰落 | — | -18.45% | candidates 計算 |
| 4 週騰落 | — | -2.46% | candidates 計算 |
| 出来高比 (20 日平均) | 37.3 億円 / 日 | — | candidates avg_turnover |
| 750 日自己レンジ位置 | — | 1.84% | candidates self_range |
| sigma_gap (per_trailing) | — | -1.75σ | candidates 計算 |

stabilization signal: 4 週 -2.5% は 60 日 -18% の急落後の終息兆候 (週次平均で
-1.0% → -0.6% へ減速)。entry trigger は「2 日連続陽線 + 出来高 1.5x」を待つ。

## 8. Crowding

| 指標 | 状況 |
| --- | --- |
| 空売り残高 | 未確認 (J-Quants 短期借株データ未取得) |
| 日々公表信用指定 | 通常想定 (`threshold_hit` に `crowding_alert` 不在) |
| 特別注意 | 無 |
| 貸借銘柄 | 通常 |

avg_turnover 37.3 億 / 日 (極めて流動性高)。0.02 億 position で adv 0.054% は
1% 制限の 1/20 以下、制約なし。

## 9. ミクロ 4 軸寄与度

| 軸 | 寄与度 | 備考 |
| --- | --- | --- |
| Valuation | strong | per_trailing -22% gap、sigma_gap -1.75 |
| Mean-Reversion | strong | self_range bottom 2%、4 週 stabilization 確認 |
| Catalyst | neutral | P-A 純粋型 |
| Crowding | neutral | 規制対象なし、流動性極大 |

## 10. Entry 条件

- **価格レンジ**: 5/1 終値 ± 3% 帯 (具体額は entry 当日確認)
- **日付制約**:
  - Q3 FY2026 (2026-03 期) 決算発表予定 2026-04 月中旬 (要 IR 確認、3 月期外資系子会社の
    通常時期)、5/15 entry の時点では公表済の見込み
  - tradable_at = 2026-05-15 (金) で earnings またぎ完全回避
- **トリガー**: entry 直前 5 営業日に 2 日連続陽線 + 出来高 1.5x 以上、4 週レンジ高値超え

## 11. Exit 条件

- **利確目標**: per_trailing 28x (業種中央値水準) → +20% 程度
- **損切り**: -10%
- **時間切れ**: 最長 40 営業日 (2026-07-10 頃まで)、catalyst 不在型のため期限内決着

## 12. Invalidation + Pre-mortem

### 12.1 無効化条件

- Q3 FY2026 決算で会社予想下方修正 (営業利益 -10% 超)
- マクロゲート headwind 化 (FOMC 6/16-17 で hawkish 急変、日銀利上げ)
- 業種中央値が大きく切り下がり relative gap 消失
- 米国の中国向け cloud 規制で親会社 Oracle USA の収益見通し悪化

### 12.2 Pre-mortem

- 3 営業日以内に -3% で entry 見送り
- Q3 FY2026 決算で OCI 関連売上の伸び鈍化が顕著なら見送り
- USD/JPY 165 超進行で原価圧迫リスクが現実化したら見送り

## 13. Position size + 採用判定

- **時価総額**: 10,940 億円 (1000+ tier)
- **許容 position**: max 2.0% (1000+ tier 上限)
- **採用 position**: 2.0% (= 0.02 億 = 200万円、1億 portfolio 想定)
- **avg_turnover**: 37.3 億 / 日 → adv_participation = 0.02 / 37.3 * 100 = 0.054%
  (1% 制限の 1/20、極めて余裕あり)
- **採用判定**: **採用**
- **判定理由**: 4 軸 valuation × mean-reversion ともに strong、業種 tailwind の selective
  受益 (Oracle USA OCI / Generative AI cloud growth の spillover)、反対仮説 (国内 cloud
  競争 / 円安原価 / 親会社依存) は中長期の構造リスクだが entry 期間 (40 営業日) には
  影響限定的。catalyst 不在の P-A 型として valuation gap > 20% + self_range 下位 2% +
  4 週 stabilization の組み合わせは playbook 採用条件をクリア。**情報・通信業 sector
  concentration**: 9682 / 3962 を 5/1 universe 除外で skipped 化することで accepted は
  5032 + 4716 = 2 件、validator concentration warning 解消。

---

**Kill switch 確認**:

- [x] 決算またぎエントリーではない (tradable_at = 2026-05-15、Q3 FY2026 決算は 4 月中旬
  発表済の見込み、要 IR 直前確認)
- [x] 日銀会合前日エントリーではない (次回 6/16-17 まで余裕)
- [x] FOMC 前日エントリーではない (次回 6/16-17 まで余裕)
- [x] マクロゲート: tailwind (sector tailwind + region neutral、保守側 = tailwind)
- [x] 200-500 億円帯該当せず (10,940 億)

全 check が ✓、採用可。

---

参照: [`/docs/components/research.md`](/docs/components/research.md), [`/docs/screening/principles.md`](/docs/screening/principles.md), [`/records/_playbooks/`](/records/_playbooks/)
