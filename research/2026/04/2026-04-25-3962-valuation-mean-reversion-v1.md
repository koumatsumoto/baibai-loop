---
ticker: "3962"
name: "チェンジホールディングス"
playbook: valuation-mean-reversion-v1
decision: accepted
screened_ref: screened/2026/04/2026-04-24.yaml
view_ref: view/2026/04/view-2026-04-24-bootstrap.md
brief_refs: []
ai-draft: true
published_at: "2026-04-25T22:00:00+09:00"
tradable_at: "2026-05-15T09:00:00+09:00"
macro_gate: neutral
position_size_oku: 0.01
valuation:
  per_forward: null
  per_trailing: 9.08
  pbr: null
  ev_ebitda: null
  p_s: null
  pcfr: null
  primary_metric: ["per_trailing"]
---

# Research: 2026-04-25 3962 チェンジホールディングス valuation-mean-reversion-v1

**Playbook**: valuation-mean-reversion-v1 (P-A)

## 1. Thesis

情報・通信業 neutral、PER trailing 9.08 で業種中央値比 -20% 以下、過去 750 日自己レンジ下位 4%、60 日下落 -17.0%。自治体 DX / ふるさと納税 (トラストバンク) の安定収益基盤に対する過剰売りの mean reversion を狙う P-A。

## 2. Macro gate

- 判定: neutral
- 業種: 情報・通信業 (view で `neutral`)
- 地域: japan-domestic neutral
- 保守側優先判定結果: neutral
- 1-2 行要約: 自治体 DX 案件は政策需要として安定。ふるさと納税は成長続くが規制リスクあり。

## 3. Valuation snapshot

| 指標 | 値 | 業種中央値 | 業種中央値比 | 過去 3 年パーセンタイル | primary |
| --- | --- | --- | --- | --- | --- |
| PER (trailing) | 9.08 | (~13) | < -20% | (下位 ~4%) | ✓ |
| PBR | null | — | — | — | |

**primary metric**: per_trailing 単独

## 4. 一時的割安の原因仮説

過去 750 日自己レンジ下位 4% で、本銘柄はグロース系から bottom-fishing 帯まで再評価される過程。ふるさと納税の規制議論の sentiment 悪化と、グロース系銘柄の循環的売り。トラストバンク事業の収益安定性は変わらず、自治体 DX 案件積み上げも継続中。

## 5. 反対仮説 - 構造的理由

**規制リスクの実体化**: ふるさと納税は度々制度見直しが議論されており、トラストバンク事業の収益基盤が政策変更で毀損するリスクがある。地方自治体 DX も予算削減で案件縮小の可能性。M&A 戦略の高値掴みリスクも noted。これらが顕在化すれば mean reversion ではなく構造的衰退が始まっている可能性。

## 6. Catalyst

P-A: catalyst なし。

## 7. Price reaction (adj close ベース)

| 対象 | 値 | 変化 |
| --- | --- | --- |
| 最新 adj close (2026-04-24) | 898 円 | — |
| 60 営業日騰落 | — | -17.0% |
| 出来高比 (20 日平均) | — | (avg 約 2.4 億円/日) |
| 750 日自己レンジ位置 | — | 4% |

## 8. Crowding

| 指標 | 状況 |
| --- | --- |
| 空売り残高 | 未確認 |
| 日々公表信用指定 | 無 |
| 特別注意 | 無 |
| 貸借銘柄 | 通常想定 |

## 9. ミクロ 4 軸寄与度

| 軸 | 寄与度 | 備考 |
| --- | --- | --- |
| Valuation | strong | per_trailing が業種比 -20% 以下 |
| Mean-Reversion | strong | 60 日 -17%、自己レンジ下位 4% (3 年最安値圏) |
| Catalyst | neutral | P-A 純粋型 |
| Crowding | weak | 流動性は中程度 (2.4 億円/日)、踏み上げ余地は限定的 |

## 10. Entry 条件

- 価格レンジ: 870 - 920 円
- 日付制約: BoJ/FOMC 前日 avoid、決算またぎ avoid
- トリガー: 決算明け + 出来高増 + 2 日連続陽線

## 11. Exit 条件

- 利確目標: 1,070 円 (+19%、業種中央値水準への 1 段戻し)
- 損切り: 800 円 (-11%、過去 750 日下限近辺)
- 時間切れ: 最長 40 営業日 (2026-07-10 頃)

## 12. Invalidation + Pre-mortem

### 12.1 無効化条件

- ふるさと納税制度大幅見直しの政策決定
- 業績下方修正
- マクロゲート headwind 化

### 12.2 Pre-mortem

- 3 営業日以内に -3% で見送り
- 流動性低下 (avg va 1.5 億円割れ) で見送り

## 13. Position size + 採用判定

- 時価総額: 663 億円
- 許容 position: max 1.0% (500-1,000 億円帯)
- 採用 position: 1.0%
- 採用判定: **採用**
- 判定理由: 自己レンジ下位 4% と過去 3 年で最深部の割安水準。反対仮説 (規制リスク) は実在するが議論段階で材料化していない。流動性は中程度、position は max 1.0% に抑制。

---

**Kill switch 確認**:

- [x] 決算またぎエントリーではない
- [x] 日銀会合前日エントリーではない
- [x] FOMC 前日エントリーではない
- [x] マクロゲート: neutral
- [x] 300-500 億円帯該当せず (663 億円)
