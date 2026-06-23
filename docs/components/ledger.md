# Ledger

`records/_ledger/` は investment memo、execution intent を append-only に正規化する decision register である。Candidates は screen fact を保持し、ledger は判断イベントと tracking state を保持する。

## 1. 役割

- `records/05-research/**/*.md` の `research_decision` を decision event として正規化する
- approved / deferred / rejected の research decision event を追跡する
- approved-but-not-submitted、submitted、filled、broker rejected などの execution state を trade lineage と接続する
- `baseline_price` と tracking horizon を market data file から更新する
- correction は既存行の書き換えではなく `event_kind: correction` の追加 event で表す

## 2. ファイル構造

- `records/_ledger/research-decisions/YYYY-MM.jsonl`: decision register の正本

JSONL は 1 行 1 event。current state は同じ `decision_event_id` / correction lineage を解決し、対象 ticker / candidate / research / trade ごとに最新の有効 event を読む。

## 3. 主なフィールド

- `decision_event_id`: decision event の安定 ID
- `event_kind`: `decision | correction`
- `decision_scope`: `research_memo | trade_execution`
- `ticker`
- `candidate_ref`
- `research_ref`
- `trade_ref`
- `research_decision`: `{outcome, posture, reason...}`
- `trade_execution_state`: `none | submitted | broker_rejected | cancelled | expired | not_filled | partially_filled | filled`
- `playbook_id` / `playbook_ref`
- `tracking`

`decision_event_id` は register 内の join key であり、trade record の `order_intent.order_intent_id`、`orders[].origin_order_intent_id` と接続する。

`candidate_ref` は candidate に紐づく decision event に付く補助ポインタ。candidates は git 外の local store のため、参照先ファイルとの照合は行わない。

## 4. Sync

手元では次を実行する。

```bash
uv run baibai-loop-position sync --root .
```

同じ入力からの再実行は同じ `decision_event_id` を更新対象として扱い、重複行を作らない。判断の訂正や無効化が必要な場合は correction event を追加する。

## 5. schema 検証

decision register は [`/records/_schemas/decision-register.json`](/records/_schemas/decision-register.json) で検証する。

```bash
uv run baibai-loop-validate --target ledger
```

## 6. dry-run 出力

`baibai-loop-position sync --dry-run` は次の prefix で差分を表示する。

- `+ decision_event_id`: 新規 event
- `~ decision_event_id`: 既存 event の再生成差分
- `! decision_event_id`: 入力側から参照できない orphan candidate / research / trade event

orphan は自動削除しない。必要なら correction event で明示的に void する。

## 8. 事故時の扱い

壊れた JSONL 行は `uv run baibai-loop-validate --target ledger` で line を確認し、該当 source artifact から再 sync する。tracking / correction log の誤記録は、新しい correction event で forward-only に修正する。
