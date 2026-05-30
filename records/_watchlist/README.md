# Watchlist

`records/_watchlist/` は、標準 screening universe や formal research の外にあるが、
ユーザー判断で継続確認したい銘柄を残す軽量な補助記録領域です。

Watch-only は採用判断ではありません。`records/04-candidates/` の fact snapshot、
`records/05-research/` の investment memo、`records/_ledger/` の decision register とは
分けて扱います。この README が watchlist の軽量運用ルールの正本です。

## 設計境界

- `records/_watchlist/` は正式な component contract ではない。`docs/components/` に contract doc を置かない。
- `baibai-loop-validate` の schema 対象にはしない。schema / validator / workflow 連携を足す場合は、watchlist を正式成分化する別 issue で判断する。
- Watch-only entry は `baibai-loop-screening select` の recommendation ではない。
- Watch-only の存在だけで formal research、trade record、ledger decision を作らない。
- Formal research へ進める場合は、標準 candidates に入った後、またはユーザーが例外扱いを明示承認した後にする。
- 株価や valuation は記録時点の snapshot として扱い、最新値の正本にしない。

## Entry の書き方

Primary list: [`watch-only.yaml`](./watch-only.yaml)

各 entry には最低限、以下を残す。

- `watch_only_id`, `ticker`, `name`, `status`, `added_on`, `added_reason`
- `decision_boundary`: formal candidate / formal research / trade candidate ではないこと
- `screening_context`: 標準 universe から外れた理由と、記録時点の snapshot
- `watch_thesis` または `valuation_notes`: なぜ後で見る価値があるか
- `review_triggers`: 次に見直す条件
- `source_refs`: 記録時に確認した CLI 実行、開示、market data

決算後などの振り返りを忘れないために GitHub issue を作った場合は、
`review_issue` に issue number / URL / title を残す。期限が明確な task issue は
`review_issue.due_on` も残す。PR で watchlist に登録しても、
この振り返り issue は原則 open のまま残す。
