---
title: "Configuration reference"
summary: "Reference for runtime configuration, environment variables, and credentials boundaries."
doc_type: reference
status: active
last_reviewed: 2026-05-13
source_paths:
  - "../../src/baibai_loop/_env.py"
  - "../../src/baibai_loop/screening/config.py"
---

# Configuration

Configuration は runtime boundary です。secret や token の値は docs に書かず、必要な変数名と責務だけを記録します。

## Principles

- secret は repository に commit しない。
- `.env` の中身を docs や issue に貼らない。
- provider payload は config loader と normalizer の境界で検証する。
- CI secret は GitHub Actions secret として扱い、docs では名前と用途だけを説明する。

## Known runtime inputs

| input | 用途 | 主な利用箇所 |
| --- | --- | --- |
| `JQUANTS_REFRESH_TOKEN` | J-Quants API access / ledger sync | `.github/workflows/ledger-sync.yml`, screening / ledger provider |
| `SCREENING_RULES_PATH` | screening rules / selection profile の既定 YAML path override | `baibai-loop-screening select`, `select-sweep`, `run` |

## Screening selection profile

`records/_config/screening-rules/2026-05-01T000000+0900.yaml` の `selection` block は、research queue を作る triage layer の既定値です。

- `selection.default_profile`: 明示 `--profile` がない場合の profile
- `selection.fast_dislocation`: 1d / 5d / 20d / 60d 下落、52 週安値距離、出来高 spike、fundamental guard の閾値
- `selection.long_hold_survivability`: equity ratio、net cash、cash、OCF / FCF、営業利益、流動性から `high|medium|low|unknown` を付ける閾値
- `selection.diversity`: recommended queue の sector / lane concentration、過去 candidates の混入上限、previous overlap warning
- `selection.ai_exposure_sector_tags`: AI exposure の sector proxy annotation

`select-sweep --profile-config <yaml>` では、次の形で任意 profile を追加できます。

```yaml
profiles:
  my-fast-lane:
    fast_dislocation:
      price_change_5d_max: -0.06
      min_fundamental_guard_count: 2
    diversity:
      max_recommended_per_sector: 1
      max_previous_candidates_in_recommended: 2
```

実装上の strictness と validation boundary は [`python-foundation.md`](./python-foundation.md) を参照します。
