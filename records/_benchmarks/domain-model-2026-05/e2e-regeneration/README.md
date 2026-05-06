# 2026-05-06 E2E Regeneration

This directory records the current end-to-end regeneration exercise for the
domain model PR.

## Current As-Of

- Requested current date: 2026-05-06
- JPX business-day result: 2026-05-06 is a market holiday
- Effective screening as-of: 2026-05-01, the latest available business-day
  candidate run already supported by the local raw screening cache

## Baseline Run

Commands:

```bash
uv run baibai-loop-screening run \
  --asof 2026-05-01 \
  --allow-stale-jpx \
  --output-path records/_benchmarks/domain-model-2026-05/e2e-regeneration/baseline-candidates.yaml \
  --force

uv run baibai-loop-screening select \
  --asof 2026-05-01 \
  --candidates records/_benchmarks/domain-model-2026-05/e2e-regeneration/baseline-candidates.yaml \
  --outlook records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml \
  --top 20 \
  > records/_benchmarks/domain-model-2026-05/e2e-regeneration/baseline-selection.yaml
```

Observed:

- Recommended research tickers in the baseline selection:
  `3632`, `6835`, `6932`, `6310`, `9470`.
- The selected queue is driven by the current screen, outlook, freshness, and
  lane ordering. It is not pinned to any prior research or trade record.

## 10-Run Parameter Sweep

The sweep in [`runs.yaml`](./runs.yaml) executes candidates generation and
research selection for ten parameter sets:

1. Baseline policy.
2. Higher liquidity threshold (`min_avg_turnover_oku: 3.0`).
3. Lower market-cap floor (`min_market_cap_oku: 50`).
4. Stricter sales growth (`sales_yoy_min: 0.10`).
5. Looser sales valuation discount (`ps_sector_gap_max: -0.30`).
6. Stricter operating cash-flow yield (`ocf_yield_min: 0.15`).
7. Stricter free cash-flow yield (`fcf_yield_min: 0.12`).
8. Deeper valuation reversion thresholds.
9. Stricter net-cash / cash-rich thresholds.
10. Sales-first research selection lane order with target max 8.

Findings:

- Candidate counts range from 243 to 390, so the screen responds materially to
  liquidity, growth, and valuation thresholds.
- Each screening run returned exit code `2`, which this CLI uses for a
  completed run with partial data-quality warnings. The artifacts are still
  written and validated; `runs.yaml` records this as
  `screening_status: partial_quality_warning` so it is not confused with a
  hard generation failure.
- The baseline selected research queue is `3632`, `6835`, `6932`, `6310`,
  `9470`. Increasing target max and moving sales first changes ordering and
  broadens the queue to 8 names.
- Higher liquidity pressure changes the selected queue to `5423`, `6266`,
  `6143`, `5410`, `6817`, which confirms liquidity policy materially changes
  the investable shortlist rather than preserving prior decisions.
- Stricter `sales_yoy_min: 0.10` cuts sales-discount-growth hits from 132 to
  60 and replaces the fifth selected name with `5036`. This confirms the
  sales-growth threshold is a high-impact business knob.
- Moving sales-discount-growth first and expanding the queue selects `9470`,
  `6310`, `6835`, `3632`, `6932`, `6619`, `6753`, `5410`. This is the primary
  current-state candidate pool for research diversification.

## Current Research Strategy

`selected_tickers` means the `baibai-loop-screening select` research triage queue.
It is not a raw candidates row flag and it is not equivalent to research approval
or order readiness.

- Core research queue: `3632`, `6835`, `6932`, `6310`, `9470`.
- Liquidity complement: `5423`, `6266`, `6143`, `5410`, `6817`.
- Growth-discount exploration sleeve: `6619`, `6753`; `5410` is promoted to a
  higher-priority complement because it appears in both the liquidity-stress and
  sales-first runs.
- Evidence-count-one names stay small until an investment memo confirms
  disconfirming evidence and payoff. The current strategy caps first orders for
  this sleeve at 75,000 yen and requires research confirmation before order
  readiness.
- Current order-ready queue is empty. Past order intents are not preserved as a
  control; fresh research is required before changing from selected/research
  queue to order-ready.

## Raw Data Added

- `records/_data/raw/screening/jquants/get_eq_bars_daily_range-end_dt-2026-05-06-start_dt-2026-04-25.json`
- `records/_data/raw/screening/jquants/get_mkt_calendar-from_yyyymmdd-20260505-to_yyyymmdd-20260506.json`
