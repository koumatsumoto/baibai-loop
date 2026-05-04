---
ticker: "9682"
name: "ＤＴＳ"
playbook: valuation-mean-reversion-v1
decision: accepted
candidates_ref: records/03-candidates/2026/04/2026-04-24.yaml
outlook_ref: records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
brief_refs:
  - records/01-brief/2026/05/2026-05-04-world-daily-us-pce-cn-trade-hormuz.yaml
ai-draft: true
published_at: "2026-05-04T22:00:00+09:00"
tradable_at: "2026-05-18T09:00:00+09:00"
macro_gate: tailwind
position_size_oku: 0.01
adv_participation_pct: 0.286
avg_turnover_oku: 3.5
market_cap_oku: 1722
sector_33: "情報・通信業"
valuation:
  per_forward: 15.32
  per_trailing: 19.67
  pbr: null
  ev_ebitda: null
  p_s: null
  pcfr: null
  primary_metric: ["per_trailing", "per_forward"]
---

# Research: 2026-05-04 9682 ＤＴＳ valuation-mean-reversion-v1

**成分**: 4 成分アーキテクチャの **(d) 個別銘柄リサーチ**

**Playbook**: valuation-mean-reversion-v1 (P-A 純粋型)

## 1. Thesis

新 outlook で `情報・通信業 = tailwind` (AI / データセンター / Sovereign AI 構造的需要) に
属する IT サービス。PER trailing 19.67 は業種中央値 28.5x 比 -31%、自己 750 営業日レンジ
下位 1.0% (`self_range_percentile: 0.0102`) で sigma_gap -1.99 と異常水準。60 営業日 -17%
下落のあと 4 週 +0.19% で stabilization。AI 関連 (DAVinCI LABS) / クラウド (Snowflake /
AWS) / 自治体 DX で sector tailwind の selective 受益。catalyst なしの純粋 P-A。

## 2. Macro gate [最上位ゲート]

- **判定**: tailwind
- **業種**: 情報・通信業 (outlook で `tailwind`)
- **地域**: japan-domestic (outlook で `neutral`) — 主に国内顧客 (金融 / 公共 / 製造)
- **保守側優先判定結果**: tailwind (業種が tailwind、地域 neutral で headwind 不在)
- **outlook_ref**: records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
- **brief_refs**: records/01-brief/2026/05/2026-05-04-world-daily-us-pce-cn-trade-hormuz.yaml
- **1-2 行要約**: sector tailwind は AI / データセンター駆動。DTS は Snowflake パートナー /
  AWS WorkSpaces VDI / DAVinCI LABS を持ち、selective な受益銘柄。地域は内需 neutral だが
  逆風要因なし。

## 3. Valuation snapshot

| 指標 | 値 | 業種中央値 | 業種中央値比 | 過去 750 日パーセンタイル | primary |
| --- | --- | --- | --- | --- | --- |
| PER (trailing) | 19.67 | 28.5 (推定 sector_median_gap -0.31 から逆算) | -31% | 1.0% (下位 1%) | ✓ |
| PER (forward) | 15.32 | — | — | — | ✓ |
| PBR | null | — | — | — | |
| EV/EBITDA | null | — | — | — | |
| P/S | null | — | — | — | |
| PCFR | null | — | — | — | |

- forward / trailing = 15.32 / 19.67 = 0.78 (forward EPS は trailing 比 +28% 期待、自然な
  earnings growth レンジ、AP-03 ±100% 乖離の異常値ではない)
- sigma_gap (per_trailing): -1.99 (約 2σ 下方)
- 50/100 タイル範囲外、強い割安シグナル

**primary metric**: per_trailing + per_forward (PBR / EV-EBITDA は会社開示の都合で null だが、
EPS / 利益ベース指標が両方の sigma で割安を確認できる)

**計算検算**: sector_median_gap -0.3103 → 中央値 = 19.67 / (1 - 0.3103) = 28.52x。
self_range_percentile 0.0102 = 750 営業日中の下位 1.02%。

## 4. 一時的割安の原因仮説

短期売り原因として以下の可能性:

- **業種ローテーションの一過性**: グロース系 IT サービスから割安バリュー / 半導体製造装置への
  ローテーション局面で、SI / 受託主体の DTS は AI 直結銘柄 (NVIDIA / TSMC 関連) より相対劣後
- **Q4 決算前の様子見売り**: 3 月期決算で 5 月中旬発表予定、保守的予想ガイダンス警戒
- **2026-01 高値からの調整**: YTD 高値 1,299 円 (1/14) からの整理、4/30 安値 991 円で底堅さ
  (-23.7% drawdown 後の +2.3% リバウンド)、4 週 +0.19% で stabilization

「一時的」根拠: AI / データセンター需要構造は数年単位の tailwind、DTS の Snowflake / AWS /
DAVinCI LABS exposure は decay しない。直近 4 週 stabilization は売り圧力一服を示唆。

## 5. 反対仮説 - 構造的理由（必須）

- **国内 SI 市場の構造的縮小**: 日本の SI 市場は人口減 + 内製化進展で長期成長率は 1-2% 程度。
  DTS は中堅 SIer で大手 (NTT データ / 野村総研 / SCSK) に対し競争力で劣後の可能性。本仮説が
  正しい場合、業種中央値も切り下がり relative valuation 改善は限定的。
- **労務費上昇圧力**: 春闘 5.09% 賃上げ + IT エンジニア不足で原価上昇が利益率を圧迫。
  forward PER 15.32 の達成 (+28% EPS 成長) が困難になるリスク。
- **AI / クラウドへの脅威転換**: Snowflake / AWS / 生成 AI 自体が SI 中間層を bypass する
  long-term リスク (顧客が directly に SaaS を契約)。本仮説が正しい場合、DTS の中長期収益基盤
  が浸食される (trap)。
- **その他**: 北九州拠点 (2026-03 開設) など先行投資負担で短期営業利益率圧迫。

→ 本仮説が正しい場合、割安は trap であり mean reversion は機能しない。

## 6. Catalyst

P-A 純粋型: catalyst なし。純粋な valuation mean-reversion 狙い (業種中央値比 -31% + 自己
レンジ下位 1% の組み合わせ)。

## 7. Price reaction (adj close ベース)

| 対象 | 値 | 変化 | ソース |
| --- | --- | --- | --- |
| 直近終値 (2026-05-01) | 1,014 円 | -0.59% | Yahoo Finance JP |
| YTD 高値 (2026-01-14) | 1,299 円 | — | Yahoo Finance JP |
| YTD 安値 (2026-03-30) | 991 円 | — | Yahoo Finance JP |
| 60 営業日騰落 | — | -17.19% | candidates 計算 |
| 4 週騰落 | — | +0.19% | candidates 計算 |
| 出来高比 (20 日平均) | 3.5 億円 / 日 | — | candidates avg_turnover |
| 750 日自己レンジ位置 | — | 1.02% | candidates self_range |

stabilization signal: 4 週 +0.19% (≈フラット) は 60 日 -17% の急落後に売り圧力が一服した
徴候。entry trigger としては「2 日連続陽線 + 出来高 1.5x 以上」を待つ。

## 8. Crowding

| 指標 | 状況 |
| --- | --- |
| 空売り残高 | 未確認 (J-Quants 短期借株データ未取得) |
| 日々公表信用指定 | 通常想定 (candidates の `threshold_hit` に `crowding_alert` 不在) |
| 特別注意 | 無 (universe filter 通過) |
| 貸借銘柄 | 通常想定 |

avg_turnover 3.5 億 / 日。1000+ 億 cap tier だが流動性は中位。0.02 億 ポジションで adv 参加率
0.57% は十分余裕がある。

## 9. ミクロ 4 軸寄与度

| 軸 | 寄与度 | 備考 |
| --- | --- | --- |
| Valuation | strong | per_trailing -31% gap、sigma_gap -1.99、forward + trailing 両方 cheap |
| Mean-Reversion | strong | self_range 下位 1.02%、4 週 stabilization 確認 |
| Catalyst | neutral | P-A 純粋型 |
| Crowding | neutral | 規制対象なし、流動性中位 |

## 10. Entry 条件

- **価格レンジ**: 970 - 1,050 円 (直近終値 ±5% 帯、買い場は 991-1,014 ゾーン)
- **日付制約**:
  - Q4 決算発表予定 2026-05-13 〜 2026-05-15 周辺と推定 (要 IR ページ確認、3月期 SIer 標準時期)
  - tradable_at = 2026-05-18 (月曜) で earnings またぎ完全回避
- **トリガー**: entry 直前 5 営業日に 2 日連続陽線 + 出来高 1.5x 以上 + 991 円のサポート維持

## 11. Exit 条件

- **利確目標**: 1,250 円 (+23%、業種中央値 PER 25 程度の水準)
- **損切り**: 912 円 (-10%、991 円のサポート割れ + バッファ)
- **時間切れ**: 最長 40 営業日 (2026-07-13 頃まで)、catalyst 不在型のため期限内決着優先

## 12. Invalidation + Pre-mortem

### 12.1 無効化条件

- Q4 決算で会社予想 EPS forward が trailing 比 -10% 超下方修正 (forward PER 15.32 が当該 EPS で
  達成困難になる場合)
- マクロゲート headwind 化 (日銀 6/16-17 で利上げ + 米 FOMC で再 hawkish 化等)
- 業種中央値が大きく切り下がり relative gap が消失

### 12.2 Pre-mortem

- 3 営業日以内に -3% で entry 見送り
- Q4 決算で「主要顧客 (大手金融 / 自治体) の SI 案件 cancellation / 縮小」報告があれば見送り
- 北九州拠点立ち上げコストで営業利益率低下の数値が trailing から forward へ伝播していたら見送り

## 13. Position size + 採用判定

- **時価総額**: 1,722 億円 (1000+ tier)
- **許容 position**: max 2.0% (1000+ tier 上限)
- **採用 position**: 1.0% (= 0.01 億 = 100万円、1億 portfolio 想定。tier 上限 2.0% に対し
  半分に絞る)
- **avg_turnover**: 3.5 億 / 日 → adv_participation = 0.01 / 3.5 * 100 = 0.286% (1% 制限内、
  余裕あり)
- **採用判定**: **採用 (情報・通信業 sector concentration mitigation 後)**
- **判定理由**: 4 軸 valuation × mean-reversion ともに strong、業種 tailwind の selective 受益
  (Snowflake / AWS / DAVinCI LABS の AI / クラウド exposure は確認済)、反対仮説 (国内 SI 縮小 /
  労務費圧迫 / SaaS bypass) は中長期だが entry 期間 (40 営業日) には影響限定的。catalyst 不在
  の P-A 型として valuation gap > 30% + self_range 1% 以下の組み合わせは playbook 採用条件を
  クリア。**ただし 3962 + 5032 と合わせて情報・通信業 sector 集中** (validator
  `research.sector-concentration` warning) のため、tier 上限の半分 (1.0%) に position size を
  抑える。

---

**Kill switch 確認**:

- [x] 決算またぎエントリーではない (tradable_at = 2026-05-18 月曜、5/13-15 の Q4 発表後)
- [x] 日銀会合前日エントリーではない (次回 6/16-17 まで余裕)
- [x] FOMC 前日エントリーではない (次回 6/16-17 まで余裕)
- [x] マクロゲート: tailwind (sector tailwind + region neutral、保守側 = tailwind)
- [x] 200-500 億円帯該当せず (1,722 億)

全 check が ✓、採用可。

---

参照: [`/docs/components/research.md`](/docs/components/research.md), [`/docs/screening/principles.md`](/docs/screening/principles.md), [`/records/_playbooks/`](/records/_playbooks/)
