---
title: "Review runbook"
summary: "Operational entry point for post-trade reviews and monthly retros."
doc_type: operation
status: active
last_reviewed: 2026-05-04
related_docs:
  - "../components/reviews.md"
  - "../screening/failure-taxonomy.md"
  - "./task-runbook.md"
---

# Review runbook

Reviews は trade 後の forward-only 検証と monthly retro を扱います。contract は [`../components/reviews.md`](../components/reviews.md) を正本とします。

## Due review gate の棚卸し

1. `uv run baibai-loop-ledger review-gates` で、open position の forward review gate（+15bd / +30bd）のうち target 営業日が到来済みで `review_state != completed` のものを列挙する。`--asof YYYY-MM-DD` で評価日を固定できる。
2. 営業日カレンダは J-Quants market calendar を primary とし、cache に無い場合は Monday-Friday の近似で target を解決する（祝日は控除しない近似 trigger）。
3. 列挙された gate ごとに individual review を作り、完了したら対応する trade の `review_state` を `completed` に更新する。決算起点の即時 review gate は [`task-runbook.md`](./task-runbook.md) の task issue 側で管理し、本コマンドの forward gate と二重管理しない。

## Individual review

1. 対応する trade と research を確認する。
2. +15 / +30 営業日など、component doc で定義されたタイミングで review を作る。`uv run baibai-loop-ledger benchmark` で、open position の forward return・benchmark proxy return・relative を J-Quants から算出できる（benchmark は日経225 ETF proxy `1321`。詳細は [`../reference/data-sources.md`](../reference/data-sources.md) §Benchmark proxy）。
3. 価格は J-Quants を primary source とする。取得できない場合は `records/_market-data/` の fallback observation を使い、本文の `Price evidence` に source URL、取得日時、評価日、price basis、benchmark と同一 basis か、provisional かを残す。
4. daily close と intraday last を混ぜない。corporate action が期間内にあり adjusted basis が確認できない場合は、outcome を provisional とし、classification を急がない。
5. 既存保有に決算後の即時 review gate がある場合は、[`task-runbook.md`](./task-runbook.md) に従い、個別タスク issue として管理する。
6. 成功要因と失敗要因は [`../screening/failure-taxonomy.md`](../screening/failure-taxonomy.md) に合わせて分類する。

## Monthly retro

1. 対象月の approved / deferred / rejected decisions と submitted / filled / closed execution records を集計する。
2. J-Quants が使えない価格を fallback で補う場合、`records/_market-data/` の observation で同一評価日の銘柄価格と benchmark を同じ basis でそろえ、retro 本文に `Price evidence` を残す。basis が揃わない場合は provisional として扱う。
3. playbook 改訂はサンプル数と failure mode を確認してから判断する。
4. active playbook の改訂が必要なら別 issue / PR として扱う。

## After writing

```bash
uv run baibai-loop-validate
```
