---
ticker: "5410"
name: "合同製鐵"
playbook: valuation-mean-reversion-v1
decision: accepted
screened_ref: records/03-screened/2026/04/2026-04-24.yaml
outlook_ref: records/02-outlook/2026/04/outlook-2026-04-24-bootstrap.md
brief_refs: []
ai-draft: true
published_at: "2026-04-25T22:00:00+09:00"
tradable_at: "2026-05-15T09:00:00+09:00"
macro_gate: neutral
position_size_oku: 0.01
adv_participation_pct: 0.43
market_cap_oku: 585
sector_33: "鉄鋼"
valuation:
  per_forward: 5.87
  per_trailing: 7.62
  pbr: null
  ev_ebitda: null
  p_s: null
  pcfr: null
  primary_metric: ["per_forward", "per_trailing"]
---

# Research: 2026-04-25 5410 合同製鐵 valuation-mean-reversion-v1

**成分**: 4 成分アーキテクチャの **(d) 個別銘柄リサーチ**

**Playbook**: valuation-mean-reversion-v1 (P-A)

## 1. Thesis

マクロは sector neutral だが地域 `japan-external-demand` tailwind に整合する鉄鋼で、PER (forward) 5.87 / trailing 7.62 と業種中央値比 -20% 以下、過去 750 日自己レンジ下位 14%、60 日下落 -15.5% (adj close ベース)。シクリカル業種特有の過剰売りの mean reversion を狙う純粋 P-A。

## 2. Macro gate

- 判定: neutral
- 業種: 鉄鋼 (outlook で `neutral`)
- 地域: `japan-external-demand` tailwind (USD/JPY 159 台 + 米最終需要強)
- 保守側優先判定結果: neutral
- outlook_ref: records/02-outlook/2026/04/outlook-2026-04-24-bootstrap.md
- 1-2 行要約: 業種は neutral だが外需地域は tailwind。鉄鋼は輸出比率が高く tailwind の恩恵を受けやすい。headwind ではないので採用可。

## 3. Valuation snapshot

| 指標 | 値 | 業種中央値 | 業種中央値比 | 過去 3 年パーセンタイル | primary |
| --- | --- | --- | --- | --- | --- |
| PER (forward) | 5.87 | (~7.5) | < -20% | (下位 ~15%) | ✓ |
| PER (trailing) | 7.62 | (~9.5) | < -20% | (下位 ~15%) | ✓ |
| PBR | null | — | — | — | |
| EV/EBITDA | null (EDINET unavailable) | — | — | — | |
| P/S | null | — | — | — | |
| PCFR | null | — | — | — | |

**primary metric**: per_forward + per_trailing (PBR 系は EDINET 未整備で null)

## 4. 一時的割安の原因仮説

鉄鋼セクター rotation の一過性 + 鉄鉱石/原料炭価格変動の短期反映。直近 60 日で adj close -15.5%、過去 750 日自己レンジ下位 14% で、循環的な売られ過ぎ。USD/JPY 159 台の円安と米最終需要の強さが下期にかけて利益寄与する公算が高い。

## 5. 反対仮説 - 構造的理由

**構造的な需要縮小**: 国内建設・自動車向け鋼材需要は人口減・電動化で構造的に縮小傾向。中国過剰生産による市況圧迫も継続リスク。本仮説が正しい場合、業種中央値も切り下がっていく中で「相対割安」が trap になる。鉄スクラップ価格と電力コスト変動リスクも常時。

## 6. Catalyst

P-A: catalyst なし (純粋な valuation mean-reversion 狙い)。直近の決算 (FY2026 3月期本決算) が 5 月中旬発表予定 → 決算結果次第で短期的に再評価材料化する可能性はあるが本研究の前提とはしない。

## 7. Price reaction (adj close ベース)

| 対象 | 値 | 変化 |
| --- | --- | --- |
| 最新 adj close (2026-04-24) | 3,410 円 | — |
| 4 週騰落 | — | (要再計算) |
| 60 営業日騰落 | — | -15.5% |
| 出来高比 (20 日平均) | — | (avg 約 2.3 億円/日) |

## 8. Crowding

| 指標 | 状況 |
| --- | --- |
| 空売り残高 | 未確認 (一次ソース別途要確認) |
| 日々公表信用指定 | 無 (JPX cache 確認済) |
| 特別注意 | 無 (JPX cache 確認済) |
| 貸借銘柄 | 通常想定 |

## 9. ミクロ 4 軸寄与度

| 軸 | 寄与度 | 備考 |
| --- | --- | --- |
| Valuation | strong | per_forward / per_trailing が業種比 -20% 以下 |
| Mean-Reversion | strong | 60 日 -15.5%、自己レンジ下位 14% |
| Catalyst | neutral | P-A 純粋型、catalyst 不要 |
| Crowding | neutral | 規制対象なし、踏み上げ余地は中立 |

## 10. Entry 条件

- 価格レンジ: 3,300 - 3,500 円
- 日付制約:
  - 2026-04-27 (BoJ 結果発表前日) — エントリー禁止
  - 2026-04-28 (FOMC 開催前日) — エントリー禁止
  - 5 月決算前後 — 決算またぎ禁止 (3 月決算企業のため 5 月初〜中旬が決算)
- トリガー: 決算明け + 出来高増を確認後の 2 日連続陽線

## 11. Exit 条件

- 利確目標: 3,800 円 (+11%、業種中央値水準への回帰)
- 損切り: 3,070 円 (-10%、過去 750 日 5%下限の手前)
- 時間切れ: 最長 40 営業日 (2026-07-10 頃)

## 12. Invalidation + Pre-mortem

### 12.1 無効化条件

- 業績下方修正 (2026 年 3 月期決算で大幅減益 / 通期予想下振れ)
- 中国鉄鋼過剰生産の深刻化シグナル
- マクロゲート headwind 化 (BoJ 利上げ / 円高反転)

### 12.2 Pre-mortem

- 3 営業日以内に -3% 突破 → entry 見送り
- 2026-04-28 BoJ 結果で円高反転 (USD/JPY 156 割れ) → 外需 tailwind 喪失で見送り

## 13. Position size + 採用判定

- 時価総額: 585 億円
- 許容 position: max 1.0% (500-1,000 億円帯)
- 採用 position: 1.0%
- 採用判定: **採用**
- 判定理由: 業種比割安かつ自己レンジ下位、外需 tailwind に整合。反対仮説 (構造的需要縮小) は中長期リスクだが、40 営業日 horizon の mean-reversion には許容範囲。kill switch すべて ✓ (実エントリーは決算明け 5/15 以降想定)。

---

**Kill switch 確認**:

- [x] 決算またぎエントリーではない (実エントリーは 5/15 以降の決算明けに合わせる)
- [x] 日銀会合前日エントリーではない (4/27 を avoid)
- [x] FOMC 前日エントリーではない (4/28 を avoid)
- [x] マクロゲート: neutral (headwind ではない)
- [x] 300-500 億円帯該当せず (585 億円)

全 ✓ につき採用可。
