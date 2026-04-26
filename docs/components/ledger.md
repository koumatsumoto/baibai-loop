# Ledger

`ledger/` は research decision を正規化した JSONL の保存先である。採用と保留は
`ledger/paper/YYYY-MM.jsonl`、明示見送りは `ledger/skipped/YYYY-MM.jsonl` に置く。

## 更新タイミング

`baibai-loop-ledger sync --root .` が `research/**/*.md` を読み、同じ `ledger_id` を
upsert する。同じ入力で再実行しても重複行は作らない。価格や tracking が取れない場合は
`null` のまま残し、後続 sync で値が埋まる。

## ファイル構造

- `ledger/paper/YYYY-MM.jsonl`: `decision: accepted | pending`
- `ledger/skipped/YYYY-MM.jsonl`: `decision: skipped`
- `ledger/updates/YYYY-MM.jsonl`: tracking 更新イベント用

## 事故時の扱い

JSONL は 1 行 1 record で、`ledger_id` が主キーである。壊れた行がある場合は
`uv run baibai-loop-validate --target ledger` で該当 line を確認し、元の research packet
から再 sync する。
