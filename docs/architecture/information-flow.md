---
title: "Information flow"
summary: "How facts, analysis, decisions, trades, reviews, and playbook feedback move through Baibai-Loop."
doc_type: architecture
status: active
last_reviewed: 2026-05-04
---

# Information flow

Baibai-Loop は screening fact と analysis を分けて判断を進めます。Macro context は外部記事と統計 series を参照する分析入力であり、記事本文や監査ログは保存しません。

```text
External articles / stats series
  |
  v
records/01-macro-context/     records/04-candidates/
  |                            |
  | macro_context_ref          | candidate_ref.candidates_ref
  v                            v
records/05-research/
                                |
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

- `records/01-macro-context/` は screening 前に確認する macro analysis です。外部記事 URL と stats series を入力メタデータとして残します。
- `records/04-candidates/` は screening 結果の fact snapshot です。通過理由を分析文として書きません。
- `records/05-research/` は candidates と macro context の統合点です。候補への参照は `candidate_ref.candidates_ref` と `candidate_ref.ticker`、macro 文脈への参照は `macro_context_ref` を必須入力として扱います。
- `records/06-trades/` は採用済み research に対する執行記録です。
- `records/07-reviews/` は forward-only な検証です。retro で playbook や screening rule の改訂判断をします。

## Feedback loop

reviews からの学びは、いきなり過去最適化に使いません。月次 retro で失敗分類と再発防止を確認し、必要な場合だけ `records/_playbooks/` や screening docs の改訂 issue / PR に進めます。運用中 playbook の位置付けは [`../components/playbooks.md`](../components/playbooks.md) を参照します。
