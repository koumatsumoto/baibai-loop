---
title: "Lane cohort forward-return telemetry (2026-05)"
summary: "週次 candidates 全銘柄を evidence lane 別 cohort として forward return を機械計測した初回スコアボード。"
doc_type: reference
status: active
last_reviewed: 2026-06-10
related_docs:
  - "./replay-2026-05.md"
  - "./mechanical.md"
---

# Lane cohort forward-return telemetry (2026-05)

`#205` / `#212` の lane cohort telemetry の運用試験。replay は recommended queue（各 profile 5 銘柄）しか評価しないため、lane（playbook）の母集団そのものが forward return を生んでいるかは不可視だった。本計測は週次 candidates の**全銘柄**（366〜642 銘柄/週）を sizing-eligible な evidence lane 別 cohort に分け、1w / 4w forward return を日経225 ETF proxy（`1321`）比で集計する。

- ツール: `baibai-loop-ledger lane-cohorts`（forward return の価格基準・eval cap の意味論は [`replay-2026-05.md`](./replay-2026-05.md) と同一）
- 対象: `.cache/replay/candidates` の 2026-05 4 週（05-01 / 05-08 / 05-15 / 05-29）、eval cap `2026-06-08`
- 4w が解決済みなのは 05-01 / 05-08 の 2 週のみ。05-15 / 05-29 の 4w は eval cap 超過で未解決
- cohort は重複あり（複数 lane hit の銘柄は各 lane に計上）。集計は等加重平均。win = benchmark 比 relative > 0 の比率

## スコアボード（mean relative、pt）

### 4w（解決済み 2 週の単純平均）

| lane | 4w rel 平均 | 05-01 | 05-08 | 対 baseline |
| --- | ---: | ---: | ---: | ---: |
| cash-rich-asset-discount | **-2.14** | -3.43 | -0.85 | **+5.32** |
| fcf-yield-discount | **-3.61** | -5.24 | -1.98 | **+3.85** |
| cashflow-yield-discount | -6.36 | -7.14 | -5.58 | +1.10 |
| strict-net-cash-discount | -7.05 | -9.43 | -4.66 | +0.41 |
| all_candidates（baseline） | -7.46 | -8.79 | -6.12 | — |
| sales-discount-growth | -8.20 | -9.72 | -6.67 | -0.74 |
| valuation-reversion | **-9.63** | -11.53 | -7.72 | **-2.17** |

### 1w（4 週平均）

| lane | 1w rel 平均 |
| --- | ---: |
| fcf-yield-discount | -0.47 |
| cash-rich-asset-discount | -0.76 |
| strict-net-cash-discount | -0.99 |
| all_candidates（baseline） | -1.60 |
| cashflow-yield-discount | -1.61 |
| sales-discount-growth | -1.68 |
| valuation-reversion | -1.87 |

## 観察された事実

1. **lane 間の序列が 1w / 4w でほぼ一貫している**: cash-rich と fcf-yield が両 horizon で baseline を明確に上回り、valuation-reversion と sales-discount-growth が両 horizon で baseline を下回る。cashflow-yield は 1w で baseline と同等（-1.61 vs -1.60）、4w で +1.10pt 上
2. **valuation-reversion は両週・両 horizon で最弱**（4w win rate 9.7% / 13.9%）。selection の `_LANE_RANK` ではこの lane が最上位（rank 0）に置かれている
3. **sales-discount-growth は baseline 以下**。2026-05 の実トレード 5 件中 3 件（9682 / 9692 / 9470）はこの lane から採用され、retro で日経比 -5.2pt の劣後が記録されている。lane cohort の弱さと実トレードの劣後が同じ方向を向いている
4. cohort 規模は lane 間で大きく異なる（fcf-yield 1〜14 / cashflow-yield 183〜375）。小 cohort（n<20）の週次値は noise が大きい

## 解釈の限界（事実と分析の分離）

- 上記は **2026-05 という rally 月の 4 週（4w は 2 週）の観測事実**であり、lane の恒常的な優劣の結論ではない。market regime が変われば序列が変わる可能性が高い（regime 別の層別は今後の蓄積で行う）
- lane_order / `_LANE_RANK` / playbook の改訂判断はこのデータだけでは行わず、月次 retro でサンプルを積んでから扱う（playbook 改訂はサンプル 10 件以上の原則に従う）
- 配当を含まない price-only リターンである（[`valuation-metrics.md`](./valuation-metrics.md) §AdjustmentClose）

## 運用への組み込み

毎週の screening 後に実行し、月次 retro の一次資料として artifact を蓄積する:

```bash
uv run baibai-loop-ledger lane-cohorts \
  --candidates-root records/04-candidates \
  --horizons 1,4 --out .cache/replay/lane-cohorts-latest.yaml
```

直近週の horizon は eval cap 未到達で `resolved 0` になる。bars が貯まった後（retro 時点）に再実行すれば同じ artifact が forward-only で埋まる。
