# Ledger

`records/_ledger/` は research decision を正規化した JSONL の保存先である。採用・保留・見送りを
後から retro できる最小 record に変換し、+15/+30 営業日後の価格追跡もここに集約する。

## 1. 役割

- `records/04-research/**/*.md` の `decision` を paper/skipped ledger に正規化する
- 同じ `ledger_id` を upsert し、同じ入力の再実行で重複行を作らない
- `baseline_price` と `tracking.plus_15bd` / `tracking.plus_30bd` を J-Quants daily から更新する
- tracking が更新された場合は `records/_ledger/updates/YYYY-MM.jsonl` に event log を残す

## 2. ファイル構造

- `records/_ledger/paper/YYYY-MM.jsonl`: `decision: accepted | pending`
- `records/_ledger/skipped/YYYY-MM.jsonl`: `decision: skipped`
- `records/_ledger/updates/YYYY-MM.jsonl`: `{ledger_id, field, old, new, observed_at}` の更新イベント。
  追跡対象 field は `baseline_price`, `adjustment_applied`, `tracking`, `decision`,
  `macro_gate`, `adv_participation_pct` の 6 個。新規 record の作成時には event を残さず、
  既存 record の値が変わった時のみ追記する。

## 3. ledger_id

`ledger_id` は以下の形式で固定する。

- paper: `paper-{decision_date:YYYYMMDD}-{ticker}-{playbook_short}`
- skipped: `skipped-{decision_date:YYYYMMDD}-{ticker}-{playbook_short_or_default}`

例: `paper-20260425-2767-vmean`

## 4. 必須フィールド

paper/skipped ともに以下を持つ。取得不能な価格・出来高系は `null` を許容する。

- `ticker` / `name` / `decision` / `playbook`
- `candidates_ref` / `research_ref`
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

ledger JSONL は [`/records/_schemas/ledger-paper-v1.json`](/records/_schemas/ledger-paper-v1.json) と
[`/records/_schemas/ledger-skipped-v1.json`](/records/_schemas/ledger-skipped-v1.json) で検証する。
手元では次を実行する。

```bash
uv run baibai-loop-validate --target ledger
```

## 7. 月次 retro 下書き

ledger と任意の個別 review から、月次 retro の下書きを生成する。

```bash
uv run baibai-loop-ledger retro --root . --month YYYY-MM
```

出力先は `records/06-reviews/YYYY/retro-YYYYMM.md`。個別 review がまだ無い月でも ledger 単独で生成し、
`price_missing_counts.plus_15bd` / `price_missing_counts.plus_30bd` に tracking 未解決件数を
必ず出す。既存ファイルがある場合は上書きしない。確認だけなら `--dry-run` を使う。

## 8. dry-run 出力の読み方

`baibai-loop-ledger sync --dry-run` は次の prefix で差分を表示する。

- `+ ledger_id`: 新規 record (upsert で追記される)
- `~ ledger_id`: 既存 record の値が変わる (upsert で置換される)
- `! ledger_id`: 既存 record だが今回の入力 (research / select) には現れない orphan。
  ledger は audit log のため upsert は削除しない。research packet が消えた・移動した等の
  状況で発生し、retro 集計の整合確認のための通知である。

## 9. 事故時の扱い

JSONL は 1 行 1 record で、`ledger_id` が主キーである。壊れた行がある場合は
`uv run baibai-loop-validate --target ledger` で該当 line を確認し、元の research packet
から再 sync する。`records/_ledger/updates/` は event log なので、重複や誤記録があれば該当行を
削除して次回 sync で再生成する。
