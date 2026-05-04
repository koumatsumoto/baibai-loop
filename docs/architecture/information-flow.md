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
records/01-brief/             records/03-candidates/
  |                            |
  | updated_from               | candidates_ref
  v                            v
records/02-outlook/ ------> records/04-research/
       outlook_ref              |
                                | accepted / skipped / pending
                                v
                         records/05-trades/
                                |
                                v
                         records/06-reviews/
                                |
                                v
                  records/_playbooks/ and screening rules
```

## Flow rules

- `records/01-brief/` は fact source です。解釈、予測、相場観を書きません。
- `records/02-outlook/` は brief を source として分析します。外部 URL を直接 source of truth にしません。
- `records/03-candidates/` は screening 結果の fact snapshot です。通過理由を分析文として書きません。
- `records/04-research/` は candidates と outlook の統合点です。`candidates_ref` と `outlook_ref` を必須入力として扱います。
- `records/05-trades/` は採用済み research に対する執行記録です。
- `records/06-reviews/` は forward-only な検証です。retro で playbook や screening rule の改訂判断をします。

## Feedback loop

reviews からの学びは、いきなり過去最適化に使いません。月次 retro で失敗分類と再発防止を確認し、必要な場合だけ `records/_playbooks/` や screening docs の改訂 issue / PR に進めます。運用中 playbook の位置付けは [`../components/playbooks.md`](../components/playbooks.md) を参照します。
