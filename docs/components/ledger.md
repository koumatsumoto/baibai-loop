# Ledger

`ledger/` は research decision を正規化した JSONL の保存先である。採用・保留・見送りを
後から retro できる最小 record に変換し、+15/+30 営業日後の価格追跡もここに集約する。

## 1. 役割

- `research/**/*.md` の `decision` を paper/skipped ledger に正規化する
- 同じ `ledger_id` を upsert し、同じ入力の再実行で重複行を作らない
- `baseline_price` と `tracking.plus_15bd` / `tracking.plus_30bd` を J-Quants daily から更新する
- tracking が更新された場合は `ledger/updates/YYYY-MM.jsonl` に event log を残す

## 2. ファイル構造

- `ledger/paper/YYYY-MM.jsonl`: `decision: accepted | pending`
- `ledger/skipped/YYYY-MM.jsonl`: `decision: skipped`
- `ledger/updates/YYYY-MM.jsonl`: `{ledger_id, field, old, new, observed_at}` の更新イベント

## 3. ledger_id

`ledger_id` は以下の形式で固定する。

- paper: `paper-{decision_date:YYYYMMDD}-{ticker}-{playbook_short}`
- skipped: `skipped-{decision_date:YYYYMMDD}-{ticker}-{playbook_short_or_default}`

例: `paper-20260425-2767-vmean`

## 4. 必須フィールド

paper/skipped ともに以下を持つ。取得不能な価格・出来高系は `null` を許容する。

- `ticker` / `name` / `decision` / `playbook`
- `screened_ref` / `research_ref`
- `asof_date` / `decision_date`
- `baseline_price`
- `market_cap_oku` / `avg_turnover_oku`
- `threshold_hit_count`
- `macro_gate`
- `adv_participation_pct`
- `adjustment_applied`
- `tracking.plus_15bd` / `tracking.plus_30bd`

`adv_participation_pct >= 5.0` は validate hard reject。`position_size_oku` は仮定資本
`1.0` 億円ベースで、research 本文の `採用 position: 1.0%` は `0.01` 億円として記録する。
実資本を変える場合は、過去 ledger を再生成する。

`adjustment_applied` は `adjustment_close != close` のときだけ `true` とする。単に
J-Quants の `adjustment_close` フィールドが存在するだけでは `true` にしない。

## 5. 更新タイミング

手元では次を実行する。

```bash
uv run baibai-loop-ledger sync --root .
```

日次更新は `.github/workflows/ledger-sync.yml` が平日 22:00 UTC (JST 翌 07:00) に実行する。
schedule では `--require-market-data` を付け、J-Quants token や market data が取れない場合は
workflow を失敗させる。

## 6. schema 検証

ledger JSONL は [`../../schemas/ledger-paper-v1.json`](../../schemas/ledger-paper-v1.json) と
[`../../schemas/ledger-skipped-v1.json`](../../schemas/ledger-skipped-v1.json) で検証する。
手元では次を実行する。

```bash
uv run baibai-loop-validate --target ledger
```

## 7. 事故時の扱い

JSONL は 1 行 1 record で、`ledger_id` が主キーである。壊れた行がある場合は
`uv run baibai-loop-validate --target ledger` で該当 line を確認し、元の research packet
から再 sync する。`ledger/updates/` は event log なので、重複や誤記録があれば該当行を
削除して次回 sync で再生成する。
