---
ticker: "5032"
name: "ＡＮＹＣＯＬＯＲ"
playbook: valuation-mean-reversion-v1
decision: accepted
screened_ref: screened/2026/04/2026-04-24.yaml
view_ref: view/2026/04/view-2026-04-24-bootstrap.md
brief_refs: []
ai-draft: true
published_at: "2026-04-25T22:00:00+09:00"
tradable_at: "2026-05-15T09:00:00+09:00"
macro_gate: neutral
position_size_oku: 0.02
valuation:
  per_forward: null
  per_trailing: 15.02
  pbr: null
  ev_ebitda: null
  p_s: null
  pcfr: null
  primary_metric: ["per_trailing"]
---

# Research: 2026-04-25 5032 ＡＮＹＣＯＬＯＲ valuation-mean-reversion-v1

**Playbook**: valuation-mean-reversion-v1 (P-A)

## 1. Thesis

情報・通信業 neutral、PER trailing 15.02 (グロース系業種比割安)、過去 750 日自己レンジ下位 20%、60 日下落 -35.4%。にじさんじ (VTuber 事務所) の安定キャッシュフロー基盤に対するグロース調整の mean reversion を狙う P-A。

## 2. Macro gate

- 判定: neutral
- 業種: 情報・通信業 (view で `neutral`)
- 地域: japan-domestic neutral / グローバル展開 (海外 VTuber、英語 Vtuber グループ)
- 保守側優先判定結果: neutral
- 1-2 行要約: VTuber 業界はグローバル展開、円安は海外売上の円換算寄与、headwind ではない。

## 3. Valuation snapshot

| 指標 | 値 | 業種中央値 | 業種中央値比 | 過去 3 年パーセンタイル | primary |
| --- | --- | --- | --- | --- | --- |
| PER (trailing) | 15.02 | (~19) | < -20% | (下位 ~20%) | ✓ |
| PBR | null | — | — | — | |

**primary metric**: per_trailing

## 4. 一時的割安の原因仮説

グロース系銘柄全般の循環的売り + 直近 60 日 -35% の急落で過剰反応領域。VTuber 事業はライブ配信 + グッズ + イベント + 海外展開と多角化済み。にじさんじブランドの globalize 進展は中長期 EPS 成長要因。短期需給で売られ過ぎ。

## 5. 反対仮説 - 構造的理由

**競争激化とトレンド変質**: VTuber 業界は競争激化 (ホロライブ、個人勢、海外勢) で talent retention コストが上昇傾向。視聴者層の trends 変化や IP 寿命の不確実性も constructive。M&A 後の統合難航リスクも noted。本仮説が正しい場合、業種中央値も切り下がり相対割安は trap。

## 6. Catalyst

P-A: catalyst なし。決算は四半期ごと、近々の決算で海外売上比率の伸びが示される可能性はあるが本研究の前提外。

## 7. Price reaction (adj close ベース)

| 対象 | 値 | 変化 |
| --- | --- | --- |
| 最新 adj close (2026-04-24) | 2,899 円 | — |
| 60 営業日騰落 | — | -35.4% |
| 出来高比 (20 日平均) | — | (avg 約 23.9 億円/日) |
| 750 日自己レンジ位置 | — | 20% |

## 8. Crowding

| 指標 | 状況 |
| --- | --- |
| 空売り残高 | 未確認 (グロース系で空売り需要高い可能性) |
| 日々公表信用指定 | 無 |
| 特別注意 | 無 |
| 貸借銘柄 | 通常想定 |

## 9. ミクロ 4 軸寄与度

| 軸 | 寄与度 | 備考 |
| --- | --- | --- |
| Valuation | strong | per_trailing が業種比 -20% 以下 |
| Mean-Reversion | strong | 60 日 -35%、自己レンジ下位 20% |
| Catalyst | neutral | P-A 純粋型 |
| Crowding | strong | 高流動性 (23.9 億円/日)、空売り踏み上げの可能性あり |

## 10. Entry 条件

- 価格レンジ: 2,800 - 2,950 円
- 日付制約: BoJ/FOMC 前日 avoid、決算またぎ avoid
- トリガー: 決算明け + 出来高増 + 2 日連続陽線

## 11. Exit 条件

- 利確目標: 3,400 円 (+17%、業種中央値水準への戻し)
- 損切り: 2,610 円 (-10%)
- 時間切れ: 最長 40 営業日 (2026-07-10 頃)

## 12. Invalidation + Pre-mortem

### 12.1 無効化条件

- 業績下方修正 (海外売上伸び鈍化、talent 大量離脱)
- VTuber 業界 sentiment 悪化 (大手他社不祥事 etc.)
- マクロゲート headwind 化

### 12.2 Pre-mortem

- 3 営業日以内に -3% 突破で見送り
- 主要 talent 離脱の announce があれば見送り

## 13. Position size + 採用判定

- 時価総額: 1,772 億円
- 許容 position: max 2.0% (1,000 億円超)
- 採用 position: 2.0%
- 採用判定: **採用**
- 判定理由: 高流動性 + 業種比割安 + 60 日急落で deep value 。反対仮説 (競争激化) は中期的リスクだが、40 営業日 horizon の短期 mean-reversion には許容範囲。crowding strong (空売り踏み上げ余地) で risk/reward 良好。

---

**Kill switch 確認**:

- [x] 決算またぎエントリーではない
- [x] 日銀会合前日エントリーではない
- [x] FOMC 前日エントリーではない
- [x] マクロゲート: neutral
- [x] 300-500 億円帯該当せず (1,772 億円)
