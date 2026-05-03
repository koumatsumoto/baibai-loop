---
ticker: "6590"
name: "芝浦メカトロニクス"
playbook: valuation-mean-reversion-v1
decision: pending
candidates_ref: records/03-candidates/2026/04/2026-04-24.yaml
outlook_ref: records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
brief_refs:
  - records/01-brief/2026/05/2026-05-03-world-weekly-fomc-boj-hold.yaml
ai-draft: true
published_at: "2026-05-04T19:00:00+09:00"
tradable_at: "2026-05-15T09:00:00+09:00"
macro_gate: tailwind
position_size_oku: 0.005
adv_participation_pct: 0.585
market_cap_oku: 693
sector_33: "電気機器"
valuation:
  per_forward: 30.14
  per_trailing: 7.35
  pbr: 1.24
  ev_ebitda: null
  p_s: null
  pcfr: null
  primary_metric: ["per_trailing", "pbr"]
---

# Research: 2026-05-04 6590 芝浦メカトロニクス valuation-mean-reversion-v1

**成分**: 4 成分アーキテクチャの **(d) 個別銘柄リサーチ** ([`/docs/components/research.md`](/docs/components/research.md))

**Playbook**: valuation-mean-reversion-v1 (P-A)

## 1. Thesis

マクロは AI / 半導体 capex 構造需要 + 電気機器 sector tailwind の業種に属し、PER trailing 7.35 は業種比 -77% / 自己 750 日下位 4.1% / sigma -1.04 の深い valuation 割安。直近 60 営業日 -81.3% の過剰売りに対し 4 週 +8.9% で反発開始 signal、純粋な valuation mean-reversion 狙い (P-A)。ただし forward PER 30.14 が来期減益を市場織り込み済みである点は反対仮説の中核。

## 2. Macro gate [最上位ゲート]

- **判定**: tailwind
- **業種**: 電気機器 (outlook 2026-05-04 で `tailwind`)
- **地域**: japan-external-demand (outlook で `tailwind`)
- **保守側優先判定結果**: tailwind (両方 tailwind なので最上位 tailwind 確定)
- **outlook_ref**: records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
- **brief_refs**: records/01-brief/2026/05/2026-05-03-world-weekly-fomc-boj-hold.yaml
- **1-2 行要約**: TSMC 1Q26 Capex $52-56B レンジ上限 + NVIDIA Sovereign AI 300 億 USD 超で半導体製造装置への構造需要は AI 軸 dominance、円安水準 (USD/JPY 156-159) 維持で日本装置メーカーの輸出採算は堅持。芝浦メカトロは AI 関連先端プロセス向けの洗浄・熱処理装置で TSMC / Samsung / Kioxia への露出があり、業種 macro tailwind を享受。

## 3. Valuation snapshot (4 軸評価表、Valuation 軸)

| 指標 | 値 | 業種中央値 | 業種中央値比 | 過去 3 年パーセンタイル | primary |
| --- | --- | --- | --- | --- | --- |
| PER (trailing) | 7.35 | (~32.4) | -77.3% | 4.1% (下位 4.1%) | ✓ |
| PER (forward) | 30.14 | — | — | — (会社予想ベースで来期減益) | |
| PBR | 1.24 | (~1.99) | -37.8% | 4.1% (下位 4.1%) | ✓ |
| EV/EBITDA | null (TTM 不足) | — | — | — | |
| P/S | null (TTM 不足) | — | — | — | |
| PCFR | null (TTM 不足) | — | — | — | |

**primary metric**: per_trailing + pbr。両者とも業種比 -38%/-77% の深い割安かつ自己レンジ下位 4.1%。sigma_gap は両者ともに -1.04 で sigma 1 以上の乖離。

## 4. 一時的割安の原因仮説 (P-A 必須)

- **半導体装置の需要サイクル懸念**: 60 営業日 -81.3% の急落は、半導体装置サイクルのピークアウト懸念 (forward PER 30 への減益反映) を市場が短期で織り込んだ。実際には TSMC Capex は 1Q26 で $52-56B レンジ上限まで上方修正、NVIDIA Sovereign AI 300 億 USD で AI 関連先端プロセス向け装置需要は構造的に拡大継続 (deep research 確認済み)。マクロ AI tailwind と業績見通しの間に divergence が発生。
- **業種ローテーションの一過性**: 2026 年初〜春先は油価高 / 中東ショック / FOMC タカ派でリスクオフ局面、半導体 capex 関連がグロース過熱として売られた。VIX が 3/9 ピーク 35.30 → 4 月平均 16.89 まで沈静化したことで、過剰売りの解消余地。
- **東芝関連の dynamics**: 親会社 (東芝) の事業再編 / グループ内取引比率の変動懸念で短期売り圧力が乗った可能性。これが本仮説 4-5 で扱う反対仮説の素材にもなる。
- **流動性枯渇局面の technical 売り**: 自己レンジ下位 4.1% は技術的に過剰売り、平均出来高 85.4 億/日は十分な流動性で踏み戻り余地あり。

**本銘柄の原因仮説総合**: AI / 半導体 capex 構造需要の trend と短期業績懸念 (装置サイクル / 親会社 dynamics) の divergence が valuation を歴史的下位水準まで押し下げた。マクロ tailwind 確認 (TSMC Capex 上方、NVIDIA Sovereign AI) で短期懸念が剥落すれば、業種中央値比 -77% の trailing PER は構造的に持続不能で mean reversion を起こす公算。

## 5. 反対仮説 - 構造的理由 (必須)

- **構造的な成長鈍化 (forward PER 30 の織り込み)**: forward PER 30.14 は trailing 7.35 の 4 倍超で、市場が来期 EPS を 80% 程度減益と織り込んでいる可能性。これが正しい場合、trailing PER 7.35 は「ピーク利益で割られた一過性数値」で、来期実績で trailing が forward 水準に収斂すれば、現在水準でも valuation 割安は消える (trap)。
- **半導体製造装置の中での競争劣位**: 芝浦メカトロは Tokyo Electron (TEL) / SCREEN Holdings / Disco / Advantest と比較して**ニッチプレーヤー** (洗浄・熱処理装置)。TSMC Capex 上方の恩恵は TEL / SCREEN にまず行き、芝浦メカトロは限定的な spillover に留まる可能性。AI 軸 tailwind の sector benefit が銘柄レベルで小さい trap。
- **米中半導体規制 (中国向け装置輸出規制) 影響**: 米国の対中半導体装置輸出規制が 2025-26 年で強化、中国向けが芝浦メカトロの売上の一定割合を占める場合、中国売上消失で減益。これが forward PER 30 への減益織り込みの正体である可能性。
- **東芝関連株の corporate action リスク**: 親会社東芝の事業再編 / 株主構成変動 / TOB / 買収・売却で大株主の売り圧力が発生する可能性。流動性は十分だが、突発的な需給逆転は valuation 反転を上回る短期リスク。
- **大型受注の落ち込み**: Kioxia (旧東芝メモリ) の NAND 設備投資縮小、メモリ価格 down サイクル局面で芝浦メカトロの受注が急減する可能性。NVIDIA / TSMC の HBM / ロジック向けは堅調だが、メモリ向け装置メーカーは別 cycle。

**本銘柄の反対仮説総合 (trap 判定の基準)**: forward PER 30 の減益織り込みが「米中規制による中国売上消失 + メモリ向け縮小」の合算で恒常化するシナリオが最大の trap risk。この場合、trailing PER 7.35 は循環ピークの artifact で mean reversion は起きない。逆に、減益が一時的 (1-2 四半期) で TSMC / NVIDIA の AI capex 拡大が芝浦メカトロにも spillover する場合、市場の懸念は剥落し mean reversion が成立する。**判定の鍵は 5 月決算 (5 月中旬-下旬) の業績ガイダンス**。

## 6. Catalyst (P-A は空欄可)

P-A 純粋型のため catalyst なし (純粋な valuation mean-reversion 狙い)。

ただし、本銘柄は近い 5 月中旬-下旬に 2026 年 3 月期通期決算を発表する想定 (3 月決算企業の標準スケジュール)。決算発表は kill switch 「決算またぎ禁止」で entry を回避する制約となるため、tradable_at を 5/15 09:00 設定 (決算明け entry 想定)。

## 7. Price reaction (adj close ベース、candidates 基準日 2026-04-24)

| 対象 | 値 | 変化 | ソース |
| --- | --- | --- | --- |
| 60 営業日騰落 | — | -81.3% | candidates 由来 (J-Quants 直近 60 営業日) |
| 4 週騰落 | — | +8.9% | candidates 由来 |
| 平均出来高 (20 日) | 85.4 億円/日 | — | candidates 由来 |
| 自己 750 日レンジ位置 | — | 4.1% (下位 4.1%) | candidates per_trailing.self_range_percentile |
| 業種相対強度 | — | percentile 1.0 (top) | candidates sector_relative_strength_percentile |

**読み方**: 60 日 -81% は明確な過剰売り、自己レンジ下位 4.1% は valuation 軸でも歴史的最深圏。一方、業種相対強度 percentile 1.0 (top) は **同業種の中で最も relative strength が高い**ことを示し、4 週 +8.9% の反発開始と整合。底入れの初期 signal が確認されている状態。

## 8. Crowding

| 指標 | 現値 | 60 日推移 | ソース |
| --- | --- | --- | --- |
| 空売り残高 (対発行済株式比) | 未確認 (要 JPX 直接確認) | — | [JPX](https://www.jpx.co.jp/markets/statistics-equities/short-selling/) |
| 日々公表信用指定 | 無 (candidates 通過時点) | — | [JPX](https://www.jpx.co.jp/markets/equities/special/index.html) |
| 特別注意 | 無 | — | [JPX](https://www.jpx.co.jp/markets/equities/special-caution/index.html) |
| 貸借銘柄状態 | 通常想定 | — | [JPX](https://www.jpx.co.jp/listing/stocks/loan/index.html) |

**Note**: deep research では JPX HTML 取得が一部 403 を返却しており、空売り残高の数値検証は entry 判断時に再取得する必要。日々公表信用指定 / 特別注意 / 整理銘柄は candidates 段階の universe フィルタで除外済みであることが保証されている。

## 9. ミクロ 4 軸寄与度表

| 軸 | 寄与度 | 備考 |
| --- | --- | --- |
| Valuation | strong | per_trailing 業種比 -77% / 自己レンジ 4.1% / sigma -1.04 |
| Mean-Reversion | strong | 60 日 -81% + 4 週 +8.9% (反発開始) + 業種相対強度 percentile 1.0 |
| Catalyst | neutral | P-A 純粋型、5 月中旬-下旬決算が trigger 候補 |
| Crowding | neutral | 信用指定 / 特別注意 / 整理 該当なし、空売り残高は要追加確認 |

## 10. Entry 条件

- **価格レンジ**: 株価データは candidates に price 直接記載なし。tradable_at 直前 (5/15 09:00) に J-Quants 直近終値を確認し、終値の ±5% を entry レンジとする運用 (例: 終値 X 円なら 0.95X 〜 1.05X)。
- **日付制約**:
  - kill switch 「決算またぎ禁止」: 5 月中旬-下旬の通期決算発表を避ける (発表日確定後に tradable_at を再調整)
  - kill switch 「日銀会合前日禁止」: 6/16-17 BOJ 会合前日 6/15 は entry 禁止
  - kill switch 「FOMC 前日禁止」: 6/16-17 FOMC 前日 6/15 は entry 禁止
  - 5/12 BOJ「主な意見」公表当日は volatility 警戒で避ける推奨
- **トリガー**:
  - 決算明け (5 月発表後) の出来高 1.2x 以上、かつ
  - 2 営業日連続陽線、かつ
  - 25 日移動平均線奪回 (反発の trend 確認)

## 11. Exit 条件

- **利確目標**: 業種中央値水準への mean reversion を想定。
  - PER trailing 業種中央値水準 (~32) 半分への正常化目標 → trailing PER 16 程度
  - これは現在 7.35 から +118% 上昇 = entry 価格 +20-30% の中期目標 (3-6 か月)
  - 短期目標 (40 営業日以内) は entry 価格 +12-15% (mean reversion 第 1 段階)
- **損切り**: entry 価格 -8% (P-A の標準損切り)
- **時間切れ**: 最長 40 営業日 (2026-05-15 entry なら 2026-07-10 頃まで)

## 12. Invalidation + Pre-mortem

### 12.1 無効化条件

- **業績下方修正**: 5 月決算で会社予想 EPS が現状 forward PER 30 想定をさらに下方修正 (forward PER > 40 に拡大) → 構造的 trap 確定で即 exit
- **米中規制強化**: USTR / 米商務省から半導体製造装置の中国向け追加制限が発表 → 中国売上 visibility 悪化で原因仮説否定
- **業種中央値の切り下がり**: 電気機器全体で業種中央値 PER が大幅低下 (例: 32 → 18) → 相対割安が消える、mean reversion target 見直し
- **マクロゲート headwind 化**: 次回 outlook で電気機器 sector tailwind → headwind 反転 (例: AI capex modulation、TSMC / NVIDIA ガイダンス下方) → 即 exit
- **東芝関連 corporate action**: 親会社東芝による TOB / 株式売却 / グループ再編で需給逆転 → 一時 exit / 再評価

### 12.2 Pre-mortem (3 営業日以内の無効化シナリオ)

- entry 後 3 営業日で -3% 以上下落、かつ出来高萎縮 (20 日平均比 0.7x 以下) → mean reversion 不発で見送り、exit
- entry 直前に米 4 月 CPI (5/12) が +3.5% 以上の Major upper を示し、FOMC 利上げ復活織り込み → 全体 risk-off で外需 tailwind 崩壊リスク、新規 entry 停止
- 5/12 BOJ「主な意見」で 6 月利上げが strong consensus → USD/JPY 急進 (150 割れ) で輸出採算警戒 → entry 規模縮小 or 見送り
- TSMC / NVIDIA の月次売上 / capex announcement で AI 軸 tailwind が崩れる indication → AI 軸 dominance 反転で見送り

## 13. Position size + 採用判定

- **時価総額**: 693 億円
- **時価総額帯**: 500-1,000 億円帯 (max 1.0%)
- **adv_participation 計算**: position_size_oku 0.005 (= 0.5%) ÷ avg_turnover_oku 85.4 × 100 = **0.585%** (5.0% hard reject 閾値の 12 分の 1、流動性は十分余裕あり)
- **採用 position**: 0.5% (500-1,000 億帯では max 1.0% だが、forward PER 30 の減益懸念リスクを踏まえ保守側に絞る)
- **採用判定**: **保留 (pending)**
- **判定理由**:
  - **採用方向の根拠**:
    - macro gate tailwind + 業種相対強度 percentile 1.0 で AI / 半導体 capex tailwind の sector benefit を享受しうる
    - valuation 4.1% percentile + sigma -1.04 で歴史的最深圏の割安、mean reversion の素地は十分
    - 60 日 -81% の過剰売り + 4 週 +8.9% 反発で底入れ初期 signal
    - kill switch (決算またぎ / 日銀前日 / FOMC 前日) は tradable_at で回避可能
  - **保留方向の根拠 (= entry 確定の前に確認すべき項目)**:
    - **5 月中旬-下旬の通期決算 (会社予想 EPS) で forward PER 30 の織り込みが「一過性」か「構造的」か確認必要**。決算で来期予想 EPS が市場想定を下回れば trap 確定
    - **米中半導体規制の最新動向確認 (USTR、5 月中の発表動向)**
    - **空売り残高の正確な数値確認 (JPX 直接取得)**
    - **5/12 BOJ「主な意見」と米 4 月 CPI で macro gate の reweight 必要**
  - **判定**: 上記 4 点を 5 月中旬までに確認し、確認結果次第で `accepted` に格上げするか、`skipped` に降格するか確定する。本 packet は **pending** として ledger に登録し、次回 cycle で再評価する

---

**Kill switch 確認**:

- [x] 決算またぎエントリーではない (tradable_at 5/15 で 5 月中旬-下旬決算明けを想定)
- [x] 日銀会合前日エントリーではない (次回 BOJ 6/16-17 会合の前日 6/15 は entry 禁止、tradable 期間中の制約として明記)
- [x] FOMC 前日エントリーではない (FOMC 6/16-17 と同様)
- [x] マクロゲート: tailwind (電気機器 = tailwind)
- [x] 200-500 億円帯 P-A 制約は該当せず (693 億で 500-1,000 億帯)

---

参照: [`/docs/components/research.md`](/docs/components/research.md), [`/docs/screening/principles.md`](/docs/screening/principles.md), [`/records/_playbooks/valuation-mean-reversion-v1.md`](/records/_playbooks/valuation-mean-reversion-v1.md), [`/records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml`](/records/02-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml)
