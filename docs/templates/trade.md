---
ticker: "XXXX"
name: "..."
research_ref: records/04-research/YYYY/MM/YYYY-MM-DD-<ticker>-<playbook>.md
order_date: "YYYY-MM-DD" | null
expected_fill_at: "ISO 8601" | null
order_price_guard_yen: 数値 | null
order_quantity: 整数 | null
guarded_max_notional_yen: 数値 | null
guarded_max_real_concentration_pct: 数値 | null
guarded_max_tactical_concentration_pct: 数値 | null
order_action_required: 文字列 | null
entry_date: "YYYY-MM-DD" | null
entry_price: 数値 | null
paper_proxy_position_size_oku: 数値
paper_proxy_position_size_pct: 数値
real_capital_yen: 数値 | null
real_order_notional_yen: 数値 | null
real_concentration_pct: 数値 | null
tactical_capital_yen: 数値 | null
tactical_concentration_pct: 数値 | null
planned_exit:
  target_price: 数値 | null
  stop_loss: 数値
  time_stop_days: 40
status: ordered | open | closed
exit_date: "YYYY-MM-DD" | null
exit_price: 数値 | null
pnl_pct: 数値 | null
kill_switch_check:
  earnings_straddle: false
  boj_eve: false
  fomc_eve: false
---

# Trade: YYYY-MM-DD XXXX [銘柄名]

**成分**: 4 成分アーキテクチャの下流 **trades**（[`/docs/components/trades.md`](/docs/components/trades.md)）

**Research source**: [records/04-research/YYYY/MM/YYYY-MM-DD-*-*.md](...)

## 1. Order / Entry

### 1.1 Entry reason（research から）

- **Thesis**（research から転写、短縮）: [1-2 段落]
- **Playbook**: [valuation-reversion | strict-net-cash-discount | fcf-yield-discount | cash-rich-asset-discount | cashflow-yield-discount | sales-discount-growth]
- **Macro gate**: [tailwind | neutral]
- **Primary valuation metric**: [per_forward + pbr 等]

### 1.2 Order / Entry triggers

実際に order / entry した条件:

- [価格レンジ到達 / 特定日 / 出来高増 / etc.]

### 1.3 Order / Entry log

| 注文日 | expected fill | 数量 | 注文種別 | 参照価格 / 約定価格 | 手数料 | 備考 |
| --- | --- | ---: | --- | ---: | --- | --- |
| YYYY-MM-DD | YYYY-MM-DD HH:MM | XX 株 | 成行 / 指値 | XXXXX 円 | XX 円 | ordered では参照価格、open では約定価格 |

**Entry price (加重平均)**: XXXXX 円 / ordered の場合は未約定

### 1.4 Position

- Paper proxy size: X.X% / X.XXXX 億円
- Real concentration: X.X%（実資金全体を使った場合のみ）
- Tactical concentration: X.X%（一時的な投入上限を置く場合のみ）
- Guarded max concentration: real X.X% / tactical X.X%（価格 guard を置く場合のみ）
- Stop loss: XXXXX 円（-X%）
- Target: XXXXX 円（+X%）
- Time stop: YYYY-MM-DD まで（最長 40 営業日）

### 1.5 Kill switch 確認

- [x] 決算またぎエントリーではない
- [x] 日銀会合前日エントリーではない
- [x] FOMC 前日エントリーではない

## 2. 保有中ログ（随時追記、exit までのメモ）

### YYYY-MM-DD

- [重大な変化があった場合のメモ]
- [マクロ変化、決算発表接近、crowding 変化、価格動向、無効化条件監視]

## 3. Exit

### 3.1 Exit reason

[利確 / 損切り / 時間切れ / 無効化 / kill switch reversal / マクロゲート headwind 化]

- [詳細説明]

### 3.2 Exit log

| 時刻 | 数量 | 単価 | 手数料 |
| --- | --- | --- | --- |
| HH:MM | XX 株 | XXXXX 円 | XX 円 |

**Exit price (加重平均)**: XXXXX 円

### 3.3 P&L

- **損益率**: X.X%
- **保有営業日数**: XX 日
- **手数料・税考慮前**: +XXXXX 円
- **手数料・税考慮後**: +XXXXX 円

## 4. Review への接続

- +15 営業日 review 予定日: YYYY-MM-DD
- +30 営業日 review 予定日: YYYY-MM-DD
- 事後 review: [`records/06-reviews/YYYY/MM/YYYY-MM-DD-XXXX.md`](/records/06-reviews/YYYY/MM/YYYY-MM-DD-XXXX.md)（決済後に作成）
