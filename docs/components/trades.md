# components/trades.md

Baibai-Loop 4 成分アーキテクチャの下流 **trades** 成分の運用仕様。research で採用された packet の執行記録。全体構造は [`../architecture-v1.md`](../architecture-v1.md) を参照。

## 1. 役割

- `research/` で採用判定された packet の **entry / exit / position / P&L を記録**
- 採用した銘柄のみ生成（見送り / 保留は `research/` 内で完結）
- `reviews/` 作成の source

## 2. 頻度

- **entry 時に作成**: research 採用判定後、実際に entry した当日
- **exit まで追記**: 決済まで同じファイルで追記（status フィールド更新）

## 3. Path と命名

```
trades/YYYY/MM/YYYY-MM-DD-<ticker>.md
```

- 日付は entry 日
- `<ticker>` は 4 桁証券コード

## 4. Front matter 必須項目

```yaml
---
ticker: "7203"
name: "トヨタ自動車"
research_ref: research/YYYY/MM/YYYY-MM-DD-<ticker>-<playbook>.md  # 必須
entry_date: "YYYY-MM-DD"
entry_price: 数値
position_size_pct: 数値                                    # 0.5 / 1 / 2 から選択
planned_exit:
  target_price: 数値 | null
  stop_loss: 数値
  time_stop_days: 40                                       # 最長 40 営業日
status: open | closed
exit_date: "YYYY-MM-DD" | null
exit_price: 数値 | null
pnl_pct: 数値 | null
kill_switch_check:                                         # entry 時に確認
  earnings_straddle: false                                 # 決算またぎではない
  boj_eve: false                                           # 日銀会合前日ではない
  fomc_eve: false                                          # FOMC 前日ではない
---
```

- `research_ref` は必須（研究なき執行を禁止）
- `kill_switch_check` の 3 項目が全て `false` でないと entry 不可
- `status`: `open` (ポジション保有中) → `closed` (決済済み)

## 5. 本文の構成

### 5.1 Entry 時

- **Entry reason**: 研究から採用判定に至った理由を 1-2 段落で要約（research の Thesis を短縮）
- **Entry triggers**: 実際に entry した条件（価格レンジ到達、特定日、出来高増など）
- **Entry log**: 実際の約定記録（時刻、数量、単価）

### 5.2 保有中（Exit まで追記）

- **Daily/weekly notes**: 重大な変化があった場合のメモ（マクロ変化、決算発表接近、crowding 変化など）
- **Invalidation watch**: 無効化条件に近づいていないか監視
- **Kill switch watch**: マクロゲートが逆風化していないか

### 5.3 Exit 時

- **Exit reason**: 利確 / 損切り / 時間切れ / 無効化 / kill switch のどれか
- **Exit log**: 約定記録
- **P&L**: 損益（%）と絶対値

## 6. reviews への接続

- 決済後 +15 営業日、+30 営業日で `reviews/YYYY/MM/YYYY-MM-DD-<ticker>.md` を作成
- 月次 retro では成功/失敗分類を集計

## 7. Kill switch 運用

entry 時 + 保有中に以下を確認:

- **決算またぎ禁止**: entry 時に次回決算発表日が保有期間内にないか確認
- **日銀会合前日禁止**: entry 時に次回 BOJ 会合日の前日ではないか確認
- **FOMC 前日禁止**: entry 時に次回 FOMC 日の前日ではないか確認
- **マクロゲート逆風化**: 保有中に view が更新され gate が `headwind` に転じた場合、即時 exit 検討（必須ではないが、無効化条件として機能）

## 8. AI の役割境界

| 作業 | AI 可 | 人間のみ |
| --- | --- | --- |
| entry/exit log の整備 | ○ | |
| kill switch check 確認補助 | ○ | **最終判定は人間** |
| P&L 計算 | ○ | |
| **実際の約定実行** | | ○ |
| **exit 判断** | | ○ |

自動発注は v1 スコープ外。

## 9. 参考

- [`../philosophy.md`](../philosophy.md): 思想
- [`../architecture-v1.md`](../architecture-v1.md): 全体構造、trades schema
- [`research.md`](./research.md): source となる research の仕様
- [`reviews.md`](./reviews.md): 接続先 reviews
- [`../templates/trade.md`](../templates/trade.md): template
