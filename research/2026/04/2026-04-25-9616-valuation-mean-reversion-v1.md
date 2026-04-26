---
ticker: "9616"
name: "共立メンテナンス"
playbook: valuation-mean-reversion-v1
screened_ref: screened/2026/04/2026-04-24.yaml
view_ref: view/2026/04/view-2026-04-24-bootstrap.md
brief_refs: []
ai-draft: true
published_at: "2026-04-25T22:00:00+09:00"
tradable_at: "2026-05-15T09:00:00+09:00"
macro_gate: neutral
valuation:
  per_forward: null
  per_trailing: 13.57
  pbr: 1.55
  ev_ebitda: null
  p_s: null
  pcfr: null
  primary_metric: ["per_trailing", "pbr"]
---

# Research: 2026-04-25 9616 共立メンテナンス valuation-mean-reversion-v1

**Playbook**: valuation-mean-reversion-v1 (P-A)

## 1. Thesis

サービス業の `neutral` ゲート、PER trailing 13.57 / PBR 1.55 で業種中央値比 -20% 以下、過去 750 日自己レンジ下位 12%、60 日下落 -17.2%。ドーミーイン (ホテル) と寮事業のキャッシュフロー基盤に対する循環的売られ過ぎの mean reversion を狙う P-A。

## 2. Macro gate

- 判定: neutral
- 業種: サービス業 (view で `neutral`)
- 地域: japan-domestic neutral
- 保守側優先判定結果: neutral
- 1-2 行要約: インバウンド需要は底堅いが、ホテル業界の競争激化が短期 headwind 要因。trader 視点は neutral。

## 3. Valuation snapshot

| 指標 | 値 | 業種中央値 | 業種中央値比 | 過去 3 年パーセンタイル | primary |
| --- | --- | --- | --- | --- | --- |
| PER (trailing) | 13.57 | (~17) | < -20% | (下位 ~12%) | ✓ |
| PBR | 1.55 | (~1.9) | < -20% | (下位 ~12%) | ✓ |
| EV/EBITDA | null | — | — | — | |

**primary metric**: per_trailing + pbr

## 4. 一時的割安の原因仮説

ホテル稼働率の足元軟化観測 + 寮事業の入居率の循環的低下による短期売り。インバウンド客は底堅く、ドーミーインのブランド力で稼働率反発の可能性。決算前の利益確定売り + 機関投資家のセクターローテーションで一時的に過剰売り。

## 5. 反対仮説 - 構造的理由

**競争激化と労働コスト上昇**: ホテル業界は外資系・ローカル新興チェーンの参入で価格競争が継続。寮事業も学生数減少 + 企業寮ニーズ縮小で成長余地が頭打ち。賃金上昇と建築コストで利益率も圧迫。本仮説が正しい場合、業種中央値は切り下がり相対割安は trap となる。

## 6. Catalyst

P-A: catalyst なし。決算 (3 月期本決算) は 5 月中旬発表予定 → 決算で稼働率改善が示されれば短期再評価ありうるが本研究の前提とはしない。

## 7. Price reaction (adj close ベース)

| 対象 | 値 | 変化 |
| --- | --- | --- |
| 最新 adj close (2026-04-24) | 2,359 円 | — |
| 60 営業日騰落 | — | -17.2% |
| 出来高比 (20 日平均) | — | (avg 約 21.6 億円/日) |
| 750 日自己レンジ位置 | — | 12% |

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
| Valuation | strong | PER+PBR 両方で業種比 -20% 以下 |
| Mean-Reversion | strong | 60 日 -17%、自己レンジ下位 12% |
| Catalyst | neutral | P-A 純粋型 |
| Crowding | neutral | 規制対象なし、流動性十分 |

## 10. Entry 条件

- 価格レンジ: 2,300 - 2,400 円
- 日付制約: BoJ/FOMC 前日 avoid、決算またぎ avoid
- トリガー: 決算明け + 出来高増 + 2 日連続陽線

## 11. Exit 条件

- 利確目標: 2,700 円 (+15%)
- 損切り: 2,120 円 (-10%)
- 時間切れ: 最長 40 営業日 (2026-07-10 頃)

## 12. Invalidation + Pre-mortem

### 12.1 無効化条件

- 業績下方修正 (ホテル稼働率や寮入居率の急低下)
- インバウンド需要鈍化 (為替反転、地政学イベント)
- マクロゲート headwind 化

### 12.2 Pre-mortem

- 3 営業日以内に -3% 突破で見送り
- 決算で稼働率予想下振れ → 見送り

## 13. Position size + 採用判定

- 時価総額: 2,077 億円
- 許容 position: max 2.0% (1,000 億円超)
- 採用 position: 2.0%
- 採用判定: **採用**
- 判定理由: PER+PBR の dual primary metric が業種比深い割安、流動性十分 (avg 21.6 億円/日)、本研究 5 銘柄中で最大の流動性と時価総額。core position に適。

---

**Kill switch 確認**:

- [x] 決算またぎエントリーではない (5/15 以降を tradable_at)
- [x] 日銀会合前日エントリーではない
- [x] FOMC 前日エントリーではない
- [x] マクロゲート: neutral
- [x] 300-500 億円帯該当せず (2,077 億円)
