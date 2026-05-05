# components/trades.md

Baibai-Loop 4 成分アーキテクチャの下流 **trades** 成分の運用仕様。research で採用された packet の執行記録。全体構造は [`../architecture.md`](../architecture.md) を参照。

## 1. 役割

- `records/04-research/` で採用判定された packet の **entry / exit / position / P&L を記録**
- 採用した銘柄のみ生成（見送り / 保留は `records/04-research/` 内で完結）
- `records/06-reviews/` 作成の source

## 2. 頻度

- **order / entry 時に作成**: research 採用判定後、実際に注文または entry した当日
- **exit まで追記**: 決済まで同じファイルで追記（status フィールド更新）

## 3. Path と命名

```
records/05-trades/YYYY/MM/YYYY-MM-DD-<ticker>.md
```

- 日付はファイル作成時点の最初の執行イベント日。未約定注文なら `order_date`、約定済み entry なら
  `entry_date`
- `ordered` から `open` に遷移しても filename は `order_date` のまま維持する。entry 日で改名しない
- `<ticker>` は 4 文字の英数字文字列

## 4. Front matter 必須項目

```yaml
---
ticker: "7203"
name: "トヨタ自動車"
research_ref: records/04-research/YYYY/MM/YYYY-MM-DD-<ticker>-<playbook>.md  # 必須
order_date: "YYYY-MM-DD" | null                         # ordered の場合は必須
expected_fill_at: "ISO 8601" | null                     # ordered の場合は次回立会予定時刻
order_price_guard_yen: 数値 | null                      # 任意。休場明け / 寄りの最大許容価格
order_action_required: 文字列 | null                     # 任意。約定前に必要な訂正 / 取消条件
entry_date: "YYYY-MM-DD" | null                         # ordered では null、open/closed では必須
entry_price: 数値 | null                                # ordered では null、open/closed では必須
paper_proxy_position_size_oku: 数値                      # 1 億円 proxy 上の建玉額
paper_proxy_position_size_pct: 数値                      # paper_proxy_position_size_oku / 1 億円 * 100
real_capital_yen: 数値 | null                            # 実資金を使った場合のみ
real_order_notional_yen: 数値 | null                     # ordered では参照価格ベース、open では約定ベース
real_concentration_pct: 数値 | null                      # real_order_notional_yen / real_capital_yen * 100
tactical_capital_yen: 数値 | null                        # 当面の様子見枠・投入上限。real_capital_yen 以下
tactical_concentration_pct: 数値 | null                  # real_order_notional_yen / tactical_capital_yen * 100
planned_exit:
  target_price: 数値 | null
  stop_loss: 数値
  time_stop_days: 40                                       # 最長 40 営業日
status: ordered | open | closed
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
- `ordered` は注文済み・未約定の状態。休場中の成行注文、寄成、引成などで価格が未確定なら
  `entry_price` を推定で埋めない
- 休場明けや寄り付きで価格上限を置く場合は、`order_price_guard_yen` と
  `order_action_required` を任意で記録し、本文の order log と一致させる。
  `order_price_guard_yen` はその trade record の数量を買ってよい最大価格であり、
  価格帯によって数量を変える場合は本文に減量条件を明示する
- `paper_proxy_*` は 1 億円 proxy の記録用。`real_*` は実資金全体の集中度を表す。両者を混同しない
- `real_capital_yen` は投資可能な実資金全体を分母にする。一時的に「今週は 100 万円まで」
  のような様子見枠を置く場合は `real_capital_yen` を小さくせず、任意 field の
  `tactical_capital_yen` / `tactical_concentration_pct` に分ける
- `kill_switch_check` の 3 項目が全て `false` でないと order / entry 不可
- `status`: `ordered` (注文済み未約定) → `open` (ポジション保有中) → `closed` (決済済み)

## 4.1 Lifecycle

| status | 必須 field | null にする field | 禁止事項 |
| --- | --- | --- | --- |
| `ordered` | `order_date`, `expected_fill_at`, `paper_proxy_position_size_*` | `entry_date`, `entry_price`, `exit_date`, `exit_price`, `pnl_pct` | 推定約定価格を `entry_price` に入れない |
| `open` | `order_date`, `entry_date`, `entry_price`, `paper_proxy_position_size_*` | `exit_date`, `exit_price`, `pnl_pct` | filename を entry 日へ改名しない |
| `closed` | `order_date`, `entry_date`, `entry_price`, `exit_date`, `exit_price`, `pnl_pct` | なし | exit 後に thesis を後付けで書き換えない |

`ordered` では stop / target は注文時の参照価格または絶対価格で置く。約定価格が参照価格から大きく
乖離した場合は、`open` 更新時に planned exit を再計算し、その理由を本文に追記する。

## 4.2 Validator

trade record は `baibai-loop-validate` (target=`trade`) で `src/baibai_loop/validate/trade.py` が enforce する。
具体的に検出する違反と対応 finding code は [`../anti-patterns.md`](../anti-patterns.md) AP-08 を参照する。

```bash
uv run baibai-loop-validate --target trade
```

主な enforced rule:

- 必須 front matter field の存在 (`trade.required-field-missing`)
- ticker 形式 (`trade.ticker-format`) と filename との一致 (`trade.filename-{date,ticker}-mismatch`)
- status 値域 (`trade.unknown-status`) と Lifecycle 表に従う null / non-null 整合 (`trade.lifecycle-*`)
- paper proxy `pct` と `oku` の整合 (`trade.paper-proxy-pct-mismatch`)
- real layer `concentration_pct` と `notional / capital` の整合 (`trade.real-concentration-mismatch`)
  および real layer の部分入力禁止 (`trade.real-concentration-missing-field`)
- tactical layer `tactical_concentration_pct` と `notional / tactical_capital` の整合
  (`trade.tactical-concentration-*`)
- real layer 集中度の hard cap (50%) / soft cap (25%) (`trade.real-concentration-{hard,soft}-cap`)

## 5. 本文の構成

### 5.1 Order / Entry 時

- **Entry reason**: 研究から採用判定に至った理由を 1-2 段落で要約（research の Thesis を短縮）
- **Order / Entry triggers**: 実際に order / entry した条件（価格レンジ到達、特定日、出来高増など）
- **Order log**: 未約定注文の記録（注文日、数量、注文種別、参照価格、expected fill）
- **Entry log**: 実際の約定記録（約定日、時刻、数量、単価）。`ordered` では推定値を書かない

### 5.2 保有中（Exit まで追記）

- **Daily/weekly notes**: 重大な変化があった場合のメモ（マクロ変化、決算発表接近、crowding 変化など）
- **Invalidation watch**: 無効化条件に近づいていないか監視
- **Kill switch watch**: マクロゲートが逆風化していないか

### 5.3 Exit 時

- **Exit reason**: 利確 / 損切り / 時間切れ / 無効化 / kill switch のどれか
- **Exit log**: 約定記録
- **P&L**: 損益（%）と絶対値

## 6. reviews への接続

- 決済後 +15 営業日、+30 営業日で `records/06-reviews/YYYY/MM/YYYY-MM-DD-<ticker>.md` を作成
- 月次 retro では成功/失敗分類を集計

## 7. Kill switch 運用

entry 時 + 保有中に以下を確認:

- **決算またぎ禁止**: entry 時に次回決算発表日が保有期間内にないか確認
- **日銀会合前日禁止**: entry 時に次回 BOJ 会合日の前日ではないか確認
- **FOMC 前日禁止**: entry 時に次回 FOMC 日の前日ではないか確認
- **マクロゲート逆風化**: 保有中に outlook が更新され gate が `headwind` に転じた場合、即時 exit 検討（必須ではないが、無効化条件として機能）

## 8. AI の役割境界

| 作業 | AI 可 | 人間のみ |
| --- | --- | --- |
| entry/exit log の整備 | ○ | |
| kill switch check 確認補助 | ○ | **最終判定は人間** |
| P&L 計算 | ○ | |
| **実際の約定実行** | | ○ |
| **exit 判断** | | ○ |

自動発注はスコープ外。

## 9. 参考

- [`../philosophy.md`](../philosophy.md): 思想
- [`../architecture/system-overview.md`](../architecture/system-overview.md): 全体構造
- [`research.md`](./research.md): source となる research の仕様
- [`reviews.md`](./reviews.md): 接続先 reviews
- [`../templates/trade.md`](../templates/trade.md): template
