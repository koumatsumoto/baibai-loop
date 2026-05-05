---
title: "Trade runbook"
summary: "Operational entry point for recording trades after accepted research decisions."
doc_type: operation
status: active
last_reviewed: 2026-05-05
related_docs:
  - "../components/trades.md"
  - "../components/research.md"
---

# Trade runbook

Trade は採用済み research packet に対する執行記録です。自動発注は行いません。contract は [`../components/trades.md`](../components/trades.md) を正本とします。

## Before writing

1. 対応する research が `decision: accepted` であることを確認する。
2. `research_ref` が実在することを確認する。
3. 成行・指値・寄成などの注文種別、休場日、次回立会日を確認する。
4. paper proxy size と real capital / real notional / real concentration を分けて記録する。
   実資金での集中度を `position_size_pct` などの paper field に混ぜない。
   一時的な投入上限を置く場合は、`real_capital_yen` を小さくせず
   `tactical_capital_yen` / `tactical_concentration_pct` に分ける。
5. 約定済みなら entry price、position size、planned exit、stop loss を記録する。
6. 注文済みだが未約定なら `status: ordered`、`entry_date: null`、`entry_price: null`、
   `expected_fill_at` で記録し、推定約定価格を入れない。約定後に `status: open` へ更新する。
7. `ordered` から `open` に更新するときは filename を変えず、`entry_date` / `entry_price` /
   `real_order_notional_yen` / `pnl_pct` の null 整合を確認する。

## After writing

```bash
uv run baibai-loop-validate
uv run baibai-loop-ledger sync --root .
```

`status: ordered` の trade では、validate 後に front matter を目視で確認する:

- `entry_date` / `entry_price` / `exit_date` / `exit_price` / `pnl_pct` が `null`
- `expected_fill_at` が次回立会時刻
- paper proxy size、real concentration、tactical concentration が別 field
