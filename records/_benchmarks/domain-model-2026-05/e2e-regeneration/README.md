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

## Raw Data Added

- `records/_data/raw/screening/jquants/get_eq_bars_daily_range-end_dt-2026-05-06-start_dt-2026-04-25.json`
- `records/_data/raw/screening/jquants/get_mkt_calendar-from_yyyymmdd-20260505-to_yyyymmdd-20260506.json`
