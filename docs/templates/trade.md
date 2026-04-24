---
ticker: "XXXX"
name: "..."
research_ref: research/YYYY/MM/YYYY-MM-DD-<ticker>-<playbook>.md
entry_date: "YYYY-MM-DD"
entry_price: 数値
position_size_pct: 0.5 | 1 | 2
planned_exit:
  target_price: 数値 | null
  stop_loss: 数値
  time_stop_days: 40
status: open | closed
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

**Research source**: [research/YYYY/MM/YYYY-MM-DD-*-*.md](...)

## 1. Entry

### 1.1 Entry reason（research から）

- **Thesis**（research から転写、短縮）: [1-2 段落]
- **Playbook**: [valuation-mean-reversion-v1 | valuation-catalyst-confirmation-v1]
- **Macro gate**: [tailwind | neutral]
- **Primary valuation metric**: [per_forward + pbr 等]

### 1.2 Entry triggers

実際に entry した条件:

- [価格レンジ到達 / 特定日 / 出来高増 / etc.]

### 1.3 Entry log

| 時刻 | 数量 | 単価 | 手数料 | 備考 |
| --- | --- | --- | --- | --- |
| HH:MM | XX 株 | XXXXX 円 | XX 円 | [成行 / 指値] |

**Entry price (加重平均)**: XXXXX 円

### 1.4 Position

- Position size: X.X%（時価総額別上限内）
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
- 事後 review: [`reviews/YYYY/MM/YYYY-MM-DD-XXXX.md`](/reviews/YYYY/MM/YYYY-MM-DD-XXXX.md)（決済後に作成）
