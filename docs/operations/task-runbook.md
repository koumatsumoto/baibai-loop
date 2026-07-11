---
title: "タスク runbook"
summary: "GitHub issue で決算後確認などの運用タスクを管理するための入口。"
doc_type: operation
status: active
last_reviewed: 2026-07-11
related_docs:
  - "../workflow/research.md"
  - "../workflow/position.md"
---

# タスク runbook

GitHub issue は、決算後確認など「将来の特定イベント後に実行する作業」の運用タスク一覧として使います。triggerの選択と全体導線は[`decision-cycle.md`](./decision-cycle.md)を正本とし、投資判断の正本はrecordsに残します。issueには実行漏れを防ぐための期限、確認項目、更新先を記録します。

## 対象

次のいずれかに該当する場合、タスク issue を作成または既存タスク issue に紐づけます。

- `thesis_decision.outcome: deferred` かつ `thesis_decision.posture: wait_for_event` の research を作った。
- 既存保有に、決算発表後の保有見直し（`review_valuation` 更新）がある。
- screening / research の途中で「YYYY-MM-DD の決算後に確認」のような実行日付きの判断待ちが発生した。

単なる調査メモ、将来いつか確認する改善案、playbook 改訂案はこの runbook の対象外です。別途 follow-up issue として扱います。

## 粒度

- 同じ期限日、同じタスク種別、同じ領域の未保有候補は、原則 1 issue にまとめます。
- 既存保有、売買判断が絡むもの、高重要候補、確認項目が大きく異なるものは個別 issue にします。
- 既に同じ期限日のタスク issue がある場合は、新規作成前にその issue へ追記できるか確認します。
- 重複 issue を見つけた場合は、残す issue に内容を移し、重複側に移管先をコメントして close します。

## Issue タイトル

タイトルは日付で検索・一覧しやすい形にします。

```text
task: YYYY-MM-DD <対象>を<イベント>後に確認する
```

例:

- `task: 2026-05-12 3964 オークネットを Q1 後に確認する`
- `task: 2026-05-15 決算後に 6310 / 6835 / 7613 を確認する`
- `task: 2026-05-15 9470 学研HDを FY2026 Q2 後に保有レビューする`

期限は title と issue body に書きます。`due:YYYY-MM-DD` のような日付 label は作りません。

## ラベル

決算後確認タスクでは、必要最小限の label だけを使います。

- `follow-up`
- `task:earnings-review`
- `area:screening`

`task:earnings-review` は、決算発表後に確認する投資調査タスクを表します。`area:screening` は screening 起点の候補確認や、その候補から発生した既存保有 review に使います。

## 本文テンプレート

```markdown
## タスク

YYYY-MM-DD の <イベント> 後に、以下を確認する。

- `<ticker> <name>`: <Q1 後確認 / Q2 後保有レビューなど>

## 背景

- 関連 candidates / research / trade / PR / issue:
- 判断待ちになった理由:
- 既存保有の場合は数量・entry・直近の保有見直し予定:

## 確認項目

### <ticker> <name>

- 一次 IR:
- 売上 / 利益 / margin:
- OCF / FCF / working capital:
- guidance / shareholder return:
- 既存 thesis を壊す条件:

## 判断

各対象について、次のいずれかを決める。

- 継続 research
- reject
- keep waiting
- 既存 position review 更新

## 更新先

- thesis:
- position（`review_valuation` / `estimate_calibration`）:
```

## 完了時

1. 会社の一次 IR を確認する。
2. issue の確認項目に沿って判断を決める。
3. 必要な records（thesis / position の `review_valuation`・`estimate_calibration`）を更新する。
4. issue に更新先 path と判断結果をコメントする。
5. タスクが完了したら issue を close する。

issue だけに判断結果を残して終わらせません。投資判断、保有判断、見送り理由は records（thesis / position）へ戻します。
