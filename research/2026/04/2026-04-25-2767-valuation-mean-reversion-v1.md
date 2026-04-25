---
ticker: "2767"
name: "円谷フィールズホールディングス"
playbook: valuation-mean-reversion-v1
screened_ref: screened/2026/04/2026-04-24.md
view_ref: view/2026/04/view-2026-04-24-bootstrap.md
brief_refs: []
ai-draft: true
published_at: "2026-04-25T22:00:00+09:00"
tradable_at: "2026-05-15T09:00:00+09:00"
macro_gate: neutral
valuation:
  per_forward: null
  per_trailing: 6.63
  pbr: null
  ev_ebitda: null
  p_s: null
  pcfr: null
  primary_metric: ["per_trailing"]
---

# Research: 2026-04-25 2767 円谷フィールズホールディングス valuation-mean-reversion-v1

**Playbook**: valuation-mean-reversion-v1 (P-A)

## 1. Thesis

マクロ neutral 業種 (卸売業) に属し、PER trailing 6.63 と業種中央値比 -20% 以下、過去 750 日自己レンジ下位 15%、60 日下落 -23.3%。ウルトラマン IP / パチンコ機販売の安定キャッシュフローに対し過剰売りの mean reversion を狙う P-A。

## 2. Macro gate

- 判定: neutral
- 業種: 卸売業 (view で `neutral`)
- 地域: japan-domestic neutral (国内消費依存)
- 保守側優先判定結果: neutral
- 1-2 行要約: 業種・地域とも neutral、headwind ではない。国内向け IP / 遊技機 + 海外 IP ライセンスの mix。

## 3. Valuation snapshot

| 指標 | 値 | 業種中央値 | 業種中央値比 | 過去 3 年パーセンタイル | primary |
| --- | --- | --- | --- | --- | --- |
| PER (trailing) | 6.63 | (~9.0) | < -20% | (下位 ~15%) | ✓ |
| PER (forward) | null (会社予想未開示) | — | — | — | |
| PBR | null | — | — | — | |
| EV/EBITDA | null | — | — | — | |

**primary metric**: per_trailing 単独

## 4. 一時的割安の原因仮説

パチンコ機販売の循環性に対する短期売り。新台投入サイクルのオフピークかつ業種ローテーションで割安化。ウルトラマン IP の海外展開 (Netflix シリーズ等) 進展は中長期収益に寄与する見込みだが、株価は短期業績で評価されている状態。

## 5. 反対仮説 - 構造的理由

**業界需要の構造的縮小**: 国内パチンコ市場は人口減・若年層離れで構造的に縮小トレンド。スマートパチンコの普及加速で機械販売は短期的に押し上げられたが、長期的な店舗減少・遊技人口減退は不可逆。本仮説が正しい場合、IP ライセンス収益では遊技機事業の縮小を相殺しきれず、業種中央値も切り下がる。

## 6. Catalyst

P-A: catalyst なし (純粋な valuation mean-reversion 狙い)。

## 7. Price reaction (adj close ベース)

| 対象 | 値 | 変化 |
| --- | --- | --- |
| 最新 adj close (2026-04-24) | 1,431 円 | — |
| 60 営業日騰落 | — | -23.3% |
| 出来高比 (20 日平均) | — | (avg 約 4.9 億円/日) |
| 750 日自己レンジ位置 | — | 15% |

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
| Mean-Reversion | strong | 60 日 -23%、自己レンジ下位 15% |
| Catalyst | neutral | P-A 純粋型 |
| Crowding | neutral | 規制対象なし |

## 10. Entry 条件

- 価格レンジ: 1,380 - 1,460 円
- 日付制約: BoJ/FOMC 前日 (4/27, 4/28) avoid、決算またぎ (3 月決算 5 月発表) avoid
- トリガー: 決算明け + 出来高増 + 2 日連続陽線

## 11. Exit 条件

- 利確目標: 1,650 円 (+15%、業種中央値水準)
- 損切り: 1,290 円 (-10%)
- 時間切れ: 最長 40 営業日 (2026-07-10 頃)

## 12. Invalidation + Pre-mortem

### 12.1 無効化条件

- 業績下方修正 (パチンコ機販売不振)
- 規制強化 (パチンコ業界規制) シグナル
- マクロゲート headwind 化

### 12.2 Pre-mortem

- 3 営業日以内に -3% で entry 見送り
- 決算で IP ライセンス収益伸び鈍化が顕著なら見送り

## 13. Position size + 採用判定

- 時価総額: 936 億円
- 許容 position: max 1.0% (500-1,000 億円帯)
- 採用 position: 1.0%
- 採用判定: **採用**
- 判定理由: 業種比深い割安と自己レンジ下位の組み合わせで純粋 P-A 適合。反対仮説 (パチンコ業界縮小) は確認したが、IP 事業との dual stream で中長期に分散がある。

---

**Kill switch 確認**:

- [x] 決算またぎエントリーではない (5/15 以降を tradable_at)
- [x] 日銀会合前日エントリーではない
- [x] FOMC 前日エントリーではない
- [x] マクロゲート: neutral
- [x] 300-500 億円帯該当せず (936 億円)
