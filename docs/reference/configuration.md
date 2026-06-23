---
title: "Configuration reference"
summary: "Reference for runtime configuration, environment variables, and credentials boundaries."
doc_type: reference
status: active
last_reviewed: 2026-05-13
source_paths:
  - "../../src/baibai_loop/_env.py"
  - "../../src/baibai_loop/screening/config.py"
  - "../../src/baibai_loop/macro/indicators/series.yaml"
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
| `JQUANTS_API_KEY` | J-Quants API v2 access（ダッシュボード発行の API キー、x-api-key）/ ledger sync | screening / ledger / stats provider |
| `SCREENING_RULES_PATH` | screening rules / selection profile の既定 YAML path override | `baibai-loop-screening select`, `run` |
| `ESTAT_APP_ID` | e-Stat API access。日本 CPI 等の公式統計を取得する `estat` provider が使う | `baibai-loop-macro`（estat provider） |

## Macro statistics cache

`baibai-loop-macro` は、macro context 作成時に確認したい米国マクロ、FRB/FRED 市場指標、ECB 由来の JPY FX を取得し、`data/indicators/macro.sqlite` に保存する。SQLite は取得 cache であり、macro context の正本ではない。

```bash
uv run baibai-loop-macro search CPI
uv run baibai-loop-macro get us.10y --start 2026-05-01 --end 2026-05-15
```

keyless で取得できる FRED CSV / FRB H.15 / ECB FX に加え、日本マクロは `estat`（日本 CPI、要 `ESTAT_APP_ID`）・`boj`（マネタリーベース xlsx、無認証）・`jquants_flows`（海外投資家フロー、要 `JQUANTS_API_KEY`）・`manual`（倒産・PMI、手動 file）で取得する。汎用の HTML / PDF scraping やニュース本文取得は対象外。

## Screening selection profile

`records/_config/screening-rules/2026-06-19T000000+0900.yaml` の `selection` block は、research recommendations を作る triage layer の既定値です。

- `selection.default_profile`: 明示 `--profile` がない場合の built-in profile。built-in は `balanced` のみで、それ以外は rules load 時に error
- `selection.liquidity`: research 推奨に適用する規模・流動性・上場期間・JPX 規制の絞り込みパラメータ。screen の scope は全普通株で、絞り込みはこの分析層パラメータだけが担う
- `selection.fast_dislocation`: 1d / 5d / 20d / 60d 下落、52 週安値距離、出来高 spike、fundamental guard の閾値。fast eligible には価格下落 trigger が必須で、52 週安値距離と出来高 spike は補助 trigger。built-in profile (`balanced`) はコード側の閾値を優先し、この YAML block は load-time contract と custom profile のベースとして扱う
- `selection.long_hold_survivability`: equity ratio、net cash、cash、OCF / FCF、営業利益、流動性から `high|medium|low|unknown` を付ける閾値
- `selection.diversity`: recommendations の sector / playbook concentration、過去 candidates の混入上限、previous overlap warning

Profile 比較が必要な場合は、`records/_config/screening-rules/2026-06-19T000000+0900.yaml` を直接編集して `select` を再実行し、output を diff する。built-in は `balanced` 一択で、experimental override は `selection-ablation` の `no_diversity` variant のように programmatic な in-process 経路でだけ提供する (`load_profile_overrides` / `--profile-config` 経路は round 2 cleanup で削除済み)。

実装上の strictness と validation boundary は [`python-foundation.md`](./python-foundation.md) を参照します。
