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
playbook_revision_decision: "据え置き" | "小改訂" | "大幅改訂"
next_cycle_changes:
  - "変更点 1"
  - "変更点 2"
price_missing_counts:
  plus_15bd: 整数
  plus_30bd: 整数
---

# Retro: YYYY-MM 月次振り返り

**成分**: Decision lifecycle の **reviews / attribution**（[`/docs/components/reviews.md`](/docs/components/reviews.md)）の月次集約

## Trade 集計

- **総 trade 数**: XX 件
- **Open trade 数**: XX 件（月末時点、未決済）
- **Closed trade 数**: XX 件
- **Missed opportunity tracking candidates**: XX 件（見送り / 保留）
- **勝敗**: Wins XX / Losses XX（closed trades のみ）
- **P&L sum**: +X.X% / -X.X%（closed trades の損益率合計）
- **Wins / Losses 内訳**（該当 trade を列挙）:

| Ticker | Playbook | Entry | Exit | PnL% | 分類 |
| --- | --- | --- | --- | --- | --- |
| XXXX | valuation-reversion | YYYY-MM-DD | YYYY-MM-DD | +X.X% | 仮説的中 |
| YYYY | cashflow-yield-discount | ... | ... | ... | ... |

## 失敗分類の集計

| 分類 | 件数 | 備考 |
| --- | --- | --- |
| 材料誤読 | XX | |
| 既に織り込み済み | XX | |
| マクロ逆風 | XX | |
| 混雑 | XX | |
| 流動性不足 | XX | |
| **ルール違反** | XX | **playbook 改訂 input にしない** |

## 成功分類の集計

| 分類 | 件数 | 備考 |
| --- | --- | --- |
| 仮説的中 | XX | |
| catalyst 反応 | XX | |
| macro tailwind | XX | |
| timing 一致 | XX | |

### 自由記述の頻出キーワード（四半期再分類 input）

全 review の `free_text` を読んで頻出キーワードを 3-5 個抽出:

- [キーワード 1]（登場回数）
- [キーワード 2]（登場回数）
- [キーワード 3]（登場回数）

四半期末（3月・6月・9月・12月）の retro で再分類候補を検討する。

## Missed opportunity / screening false negative tracking の分析

見送り / 保留した候補、および screening false negative 候補について、+15/+30 営業日の仮想パフォーマンスと relative return を集計:

| Ticker | 追跡区分 | 見送り理由 / 検出理由 | +15 日騰落 | +30 日騰落 | 判定妥当性 |
| --- | --- | --- | --- | --- | --- |
| XXXX | missed opportunity / screening false negative | Macro gate headwind | +X% | +X% | 妥当 / 偽陰性 |

**偽陰性率**: XX/XX 件（見送ったが +30 日で上昇した銘柄の比率）

**価格欠損件数**: +15bd XX 件 / +30bd XX 件

## Macro gate 判定精度

- **追い風判定銘柄の +15 日パフォーマンス**: +X.X%（平均）
- **逆風判定で見送った銘柄の +15 日パフォーマンス**: +X.X%（平均）
- **gate 判定誤り**（採用時 tailwind → 保有中 headwind に反転）: XX 件
- **outlook の更新頻度が適切だったか**: [定量評価]

## Playbook 改訂判断

- **Playbook 別サンプル数**:
  - valuation-reversion: XX 件
  - cash-rich-asset-discount: XX 件
  - cashflow-yield-discount: XX 件
  - sales-discount-growth: XX 件
- **10 件未満の playbook**: 据え置きを許容
- **改訂判断**: [据え置き | 小改訂（checklist 差分提案）| 大幅改訂]
- **改訂の根拠**: [1-2 段落]

### Checklist 差分提案

- 次周回で意識するポイント（3-5 個）を列挙:
  - [提案 1]
  - [提案 2]
  - [提案 3]

## 次周回の運用変更点

- [変更点 1]
- [変更点 2]
- [変更点 3]

変更適用対象: [playbook / screening 閾値 / outlook 更新 trigger / research 選定基準 のどれか]

### Valuation trap の経験

- 当月の valuation trap 該当 trade: XX 件
- 0 件だった場合の対応: 「サンプル不足のため valuation trap 耐性は未検証」と明記し、次周回の重点観察項目に追加

---

参照: [`/docs/components/reviews.md`](/docs/components/reviews.md), [`/docs/screening/failure-taxonomy.md`](/docs/screening/failure-taxonomy.md), [`/docs/screening/principles.md`](/docs/screening/principles.md)
