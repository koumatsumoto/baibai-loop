# components/watchlist.md

Watchlist は、標準 screening universe や formal research の外にあるが、ユーザー判断で継続確認したい銘柄を記録する support component です。

## 1. 役割

- 標準 universe に入らない銘柄を、正式な candidates / research と混ぜずに残す
- 「低流動性だが気になる」「小型すぎるが後で見たい」銘柄の漏れを防ぐ
- Formal research へ昇格できない理由と、次に見る trigger を明示する

Watchlist は採用判断ではありません。`records/04-candidates/` の fact snapshot、`records/05-research/` の investment memo、`records/_ledger/` の decision register とは分けます。

## 2. 保存場所

```text
records/_watchlist/watch-only.yaml
```

## 3. Field

- `watch_only_id`: entry の安定 ID
- `ticker` / `name` / `market` / `sector_33`
- `status`: `active` / `inactive`
- `classification`: watch-only にした理由の分類
- `decision_boundary`: formal candidate / formal research / trade candidate ではないことの明示
- `screening_context`: 標準 universe から外れた理由と記録時点の snapshot
- `valuation_notes`: watch 理由になる valuation / shareholder return の短い記録
- `review_triggers`: 次に見直す条件
- `source_refs`: 記録時に確認した CLI 実行、開示、market data

## 4. 運用ルール

- Watch-only entry は `baibai-loop-screening select` の recommendation ではない。
- Formal research へ進める場合は、標準 candidates に入った後、またはユーザーが例外扱いを明示承認した後にする。
- 株価や valuation は記録時点の snapshot であり、最新値として再利用しない。
- Watch-only の存在だけで発注・trade record を作らない。
