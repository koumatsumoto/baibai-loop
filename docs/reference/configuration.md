---
title: "Configuration reference"
summary: "Reference for runtime configuration, environment variables, and credentials boundaries."
doc_type: reference
status: active
last_reviewed: 2026-05-13
source_paths:
  - "../../src/baibai_loop/_env.py"
  - "../../src/baibai_loop/screening/config.py"
  - "../../src/baibai_loop/stats/series.yaml"
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
| `ESTAT_APP_ID` | e-Stat API access。macro stats の日本統計 adapter を追加するときに使う未実装の future provider | `baibai-loop-stats` future provider |

## Macro statistics cache

`baibai-loop-stats` は、macro context 作成時に確認したい米国マクロ、FRB/FRED 市場指標、ECB 由来の JPY FX を取得し、`data/stats/macro.sqlite` に保存する。SQLite は取得 cache であり、macro context の正本ではない。

```bash
uv run baibai-loop-stats search CPI
uv run baibai-loop-stats get us.10y --start 2026-05-01 --end 2026-05-15
uv run baibai-loop-stats macro-fragment --kind world-weekly --start 2026-05-04 --end 2026-05-10
```

v1 は keyless CSV で取得できる FRB H.15、FRED CSV、ECB FX を優先する。日本 CPI / BOJ rate などの日本マクロ統計は e-Stat / BOJ の安定 series id と認証運用が固まるまで未実装。HTML / PDF scraping やニュース本文取得は対象外。

## Screening selection profile

`records/_config/screening-rules/2026-05-01T000000+0900.yaml` の `selection` block は、research queue を作る triage layer の既定値です。

- `selection.default_profile`: 明示 `--profile` がない場合の built-in profile。`strict` / `balanced` / `loose` 以外は rules load 時に error
- `selection.fast_dislocation`: 1d / 5d / 20d / 60d 下落、52 週安値距離、出来高 spike、fundamental guard の閾値。fast eligible には価格下落 trigger が必須で、52 週安値距離と出来高 spike は補助 trigger。built-in profile (`strict` / `balanced` / `loose`) はコード側の閾値を優先し、この YAML block は load-time contract と custom profile のベースとして扱う
- `selection.long_hold_survivability`: equity ratio、net cash、cash、OCF / FCF、営業利益、流動性から `high|medium|low|unknown` を付ける閾値
- `selection.diversity`: recommended queue の sector / lane / recommendation queue concentration、過去 candidates の混入上限、previous overlap warning

`select-sweep --profile-config <yaml>` では、次の形で任意 profile を追加できます。

```yaml
profiles:
  my-fast-lane:
    fast_dislocation:
      price_change_5d_max: -0.06
      min_fundamental_guard_count: 2
      min_fundamental_guard_family_count: 2
    diversity:
      max_recommended_per_sector: 1
      max_recommended_per_queue: 3
      max_previous_candidates_in_recommended: 2
```

Profile YAML の未知 key や `queue_order` の未知 queue 名は fail-fast します。閾値名を typo した状態で `select-sweep` を成功扱いにしないためです。

実装上の strictness と validation boundary は [`python-foundation.md`](./python-foundation.md) を参照します。
