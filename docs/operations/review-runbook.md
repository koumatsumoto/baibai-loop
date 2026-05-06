---
title: "Review runbook"
summary: "Operational entry point for post-trade reviews and monthly retros."
doc_type: operation
status: active
last_reviewed: 2026-05-04
related_docs:
  - "../components/reviews.md"
  - "../screening/failure-taxonomy.md"
---

# Review runbook

Reviews は trade 後の forward-only 検証と monthly retro を扱います。contract は [`../components/reviews.md`](../components/reviews.md) を正本とします。

## Individual review

1. 対応する trade と research を確認する。
2. +15 / +30 営業日など、component doc で定義されたタイミングで review を作る。
3. 成功要因と失敗要因は [`../screening/failure-taxonomy.md`](../screening/failure-taxonomy.md) に合わせて分類する。

## Monthly retro

1. 対象月の approved / deferred / rejected decisions と submitted / filled / closed execution records を集計する。
2. playbook 改訂はサンプル数と failure mode を確認してから判断する。
3. active playbook の改訂が必要なら別 issue / PR として扱う。

## After writing

```bash
uv run baibai-loop-validate
```
