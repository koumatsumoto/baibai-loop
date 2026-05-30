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

## Individual review

1. 対応する trade と research を確認する。
2. +15 / +30 営業日など、component doc で定義されたタイミングで review を作る。
3. 価格は J-Quants を primary source とする。取得できない場合は fallback source を使い、本文の `Price evidence` に source URL、取得日時、評価日、price basis、benchmark と同一 basis かを残す。
4. daily close と intraday last を混ぜない。corporate action が期間内にあり adjusted basis が確認できない場合は、outcome を provisional とし、classification を急がない。
5. 既存保有に決算後の即時 review gate がある場合は、[`task-runbook.md`](./task-runbook.md) に従い、個別タスク issue として管理する。
6. 成功要因と失敗要因は [`../screening/failure-taxonomy.md`](../screening/failure-taxonomy.md) に合わせて分類する。

## Monthly retro

1. 対象月の approved / deferred / rejected decisions と submitted / filled / closed execution records を集計する。
2. J-Quants が使えない価格を fallback で補う場合、同一評価日の銘柄価格と benchmark を同じ basis でそろえ、retro 本文に `Price evidence` を残す。
3. playbook 改訂はサンプル数と failure mode を確認してから判断する。
4. active playbook の改訂が必要なら別 issue / PR として扱う。

## After writing

```bash
uv run baibai-loop-validate
```
