---
title: "Information flow"
summary: "How facts, analysis, decisions, trades, reviews, and playbook feedback move through Baibai-Loop."
doc_type: architecture
status: active
last_reviewed: 2026-05-04
---

# Information flow

Baibai-Loop は fact layer を先に固定し、analysis layer がその参照を持つ形で判断を進めます。AI が analysis を書く場合でも、fact は brief / candidates から辿れる必要があります。

```text
External primary sources
  |
  v
records/02-brief/             records/04-candidates/
  |                            |
  | updated_from               | candidate_ref.candidates_ref
  v                            v
records/03-outlook/ ------> records/05-research/
       outlook_ref              |
                                | approved / deferred / rejected
                                v
                         records/06-trades/
                                |
                                v
                         records/07-reviews/
                                |
                                v
                  records/_playbooks/ and screening rules
```

## Flow rules

- `records/02-brief/` は fact source です。解釈、予測、相場観を書きません。
- `records/03-outlook/` は brief を source として分析します。外部 URL を直接 source of truth にしません。
- `records/04-candidates/` は screening 結果の fact snapshot です。通過理由を分析文として書きません。
- `records/05-research/` は candidates と outlook の統合点です。候補への参照は `candidate_ref.candidates_ref` / `screen_run_id` / `ticker` / `candidate_id` の完全 join key、macro 文脈への参照は `outlook_ref` を必須入力として扱います。
- `records/06-trades/` は採用済み research に対する執行記録です。
- `records/07-reviews/` は forward-only な検証です。retro で playbook や screening rule の改訂判断をします。

## Feedback loop

reviews からの学びは、いきなり過去最適化に使いません。月次 retro で失敗分類と再発防止を確認し、必要な場合だけ `records/_playbooks/` や screening docs の改訂 issue / PR に進めます。運用中 playbook の位置付けは [`../components/playbooks.md`](../components/playbooks.md) を参照します。
