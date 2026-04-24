---
retro_month: "YYYY-MM"
total_trades: 整数
open_trades: 整数
closed_trades: 整数
skipped_candidates: 整数
wins: 整数
losses: 整数
pnl_pct_sum: 数値
failure_class_counts:
  材料誤読: 整数
  既に織り込み済み: 整数
  マクロ逆風: 整数
  混雑: 整数
  流動性不足: 整数
  ルール違反: 整数
success_class_counts:
  仮説的中: 整数
  catalyst 反応: 整数
  macro tailwind: 整数
  timing 一致: 整数
playbook_revision_decision: "v1 据え置き" | "v1.1 改訂" | "v2 開発"
next_cycle_changes:
  - "変更点 1"
  - "変更点 2"
---

# Retro: YYYY-MM 月次振り返り

**成分**: 4 成分アーキテクチャの下流 **reviews**（[`/docs/components/reviews.md`](/docs/components/reviews.md)）の月次集約

## 1. Trade 集計

- **総 trade 数**: XX 件
- **Open trade 数**: XX 件（月末時点、未決済）
- **Closed trade 数**: XX 件
- **Skipped candidates**: XX 件（見送り / 保留）
- **勝敗**: Wins XX / Losses XX（closed trades のみ）
- **P&L sum**: +X.X% / -X.X%（closed trades の損益率合計）
- **Wins / Losses 内訳**（該当 trade を列挙）:

| Ticker | Playbook | Entry | Exit | PnL% | 分類 |
| --- | --- | --- | --- | --- | --- |
| XXXX | P-A | YYYY-MM-DD | YYYY-MM-DD | +X.X% | 仮説的中 |
| YYYY | P-B | ... | ... | ... | ... |

## 2. 失敗分類の集計

| 分類 | 件数 | 備考 |
| --- | --- | --- |
| 材料誤読 | XX | |
| 既に織り込み済み | XX | |
| マクロ逆風 | XX | |
| 混雑 | XX | |
| 流動性不足 | XX | |
| **ルール違反** | XX | **playbook 改訂 input にしない** |

## 3. 成功分類の集計

| 分類 | 件数 | 備考 |
| --- | --- | --- |
| 仮説的中 | XX | |
| catalyst 反応 | XX | |
| macro tailwind | XX | |
| timing 一致 | XX | |

## 4. 自由記述の頻出キーワード（四半期再分類 input）

全 review の `free_text` を読んで頻出キーワードを 3-5 個抽出:

- [キーワード 1]（登場回数）
- [キーワード 2]（登場回数）
- [キーワード 3]（登場回数）

四半期末（3月・6月・9月・12月）の retro で再分類候補を検討する。

## 5. Skipped trade log の分析

見送り / 保留した候補について、+15/+30 営業日の仮想パフォーマンスを集計:

| Ticker | 見送り理由 | +15 日騰落 | +30 日騰落 | 判定妥当性 |
| --- | --- | --- | --- | --- |
| XXXX | Macro gate headwind | +X% | +X% | 妥当 / 偽陰性 |

**偽陰性率**: XX/XX 件（見送ったが +30 日で上昇した銘柄の比率）

## 6. Macro gate 判定精度

- **追い風判定銘柄の +15 日パフォーマンス**: +X.X%（平均）
- **逆風判定で見送った銘柄の +15 日パフォーマンス**: +X.X%（平均）
- **gate 判定誤り**（採用時 tailwind → 保有中 headwind に反転）: XX 件
- **view の更新頻度が適切だったか**: [定量評価]

## 7. Playbook 改訂判断

- **Playbook 別サンプル数**:
  - P-A: XX 件
  - P-B: XX 件
- **10 件未満の playbook**: v1 据え置きを許容（#7 から継承）
- **改訂判断**: [v1 据え置き | v1.1 改訂（checklist 差分提案）| v2 開発]
- **改訂の根拠**: [1-2 段落]

### 7.1 Checklist 差分提案（v1.1 案）

- 次周回で意識するポイント（3-5 個）を列挙:
  - [提案 1]
  - [提案 2]
  - [提案 3]

## 8. 次周回の運用変更点（3 行以内要約）

- [変更点 1]
- [変更点 2]
- [変更点 3]

変更適用対象: [playbook / screening 閾値 / view 更新 trigger / research 選定基準 のどれか]

## 9. Valuation trap の経験

- 当月の valuation trap 該当 trade: XX 件
- 0 件だった場合の対応: 「サンプル不足のため valuation trap 耐性は未検証」と明記し、次周回の重点観察項目に追加

---

参照: [`/docs/components/reviews.md`](/docs/components/reviews.md), [`/docs/screening/failure-taxonomy.md`](/docs/screening/failure-taxonomy.md), [`/docs/screening/principles.md`](/docs/screening/principles.md)
