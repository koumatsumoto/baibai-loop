# 2026-05-06 E2E Regeneration

This directory records the current end-to-end regeneration exercise for the
domain model PR. It is not a migration audit trail.

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

- `9682`: present, `sales-discount-growth`, sizing-eligible.
- `9692`: present, `sales-discount-growth`, sizing-eligible.
- `3678`: present, 4 evidence hits, all `source_status: warning` and
  `sizing_eligible: false`.
- Recommended research tickers in the baseline selection:
  `3632`, `6835`, `6932`, `6310`, `9470`.
- `3678` is not recommended after freshness-aware sizing eligibility is applied.

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
- `3678` is either absent under stricter liquidity or present with zero
  sizing-eligible evidence; it is never selected. This is the desired behavior
  for post-snapshot corporate-action risk.
- `9682` and `9692` remain sizing-eligible candidates in the baseline, but are
  not selected in the top research queue under the current 5/4 outlook and lane
  ordering. In the full baseline rank they are around rank 111 and 103
  respectively, which means the current process surfaces stronger candidates
  before them.
- The baseline selected research queue is `3632`, `6835`, `6932`, `6310`,
  `9470`. Increasing target max and moving sales first changes ordering and
  broadens the queue to 8 names.
- Stricter `sales_yoy_min: 0.10` drops `9682` from the candidate set while
  retaining `9692`. This confirms the sales-growth threshold is a high-impact
  business knob and should be changed only with benchmark review.

## Raw Data Added

- `records/_data/raw/screening/jquants/get_eq_bars_daily_range-end_dt-2026-05-06-start_dt-2026-04-25.json`
- `records/_data/raw/screening/jquants/get_mkt_calendar-from_yyyymmdd-20260505-to_yyyymmdd-20260506.json`
