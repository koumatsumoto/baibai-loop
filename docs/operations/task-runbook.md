---
title: "タスク runbook"
summary: "application DBとbaibai-engine taskで決算後確認などの運用taskを管理する入口。"
doc_type: operation
status: active
last_reviewed: 2026-07-18
related_docs:
  - "../workflow/research.md"
  - "../workflow/position.md"
---

# タスク runbook

運用 task の正本はapplication DBです。`baibai-engine task`が唯一のwriterで、決算後確認など、将来の特定eventや日付で実行する作業のcurrent stateを管理します。triggerの選択と全体導線は[`decision-cycle.md`](./decision-cycle.md)を正本とし、投資判断と注文状態はそれぞれcanonical entityとhuman-confirmed ledgerに残します。

GitHub Issueはtask管理には使わず、feature、bug、基盤改善、PR deliveryなどの開発作業に使います。taskは状態遷移履歴を持たず、DB rowにcurrent stateだけを保持します。

## 対象

次のいずれかに該当する作業を task record にします。

- `judgment.recommendation: defer` の thesis で、再確認する event が特定されている。
- 既存保有に、決算発表後の holding review 更新がある。
- screening / research の途中で、実行日付きの判断待ちが発生した。
- 注文期限や人間入力の到来後に実行する運用確認がある。

単なる調査メモ、日付のない改善案、screening rule の改訂案は対象外です。方法改善は improvement loop の開発 Issue で扱います。

## 粒度

- 同じ期限日、同じ task 種別、同じ領域の未保有候補は、原則一つにまとめます。
- 既存保有、売買判断が絡むもの、高重要候補、確認項目が大きく異なるものは個別 task にします。
- 新規追加前に`baibai-engine task list --status open`で期限、ticker、確認内容を照合し、既存taskへ統合できないか確認します。
- `task_id`は`task-YYYYMMDD-<slug>`で自動採番され、`YYYYMMDD`は作成identityです。due dateを編集してもtask_idは変えません。

fieldの型・必須項目・enumは`baibai_engine.tasks.Task`とapplication serviceのwrite-time validationが正本です。task間で`task_id`は一意です。

## Status

- `open`: 実行条件の到来待ち、または作業が未完了。
- `done`: load-bearing question への判断と必要な canonical 更新が完了。
- `dropped`: current canonical state では task の問いが成立しない、または実行不要。

完了は`baibai-engine task done <task_id>`、不要化は`baibai-engine task drop <task_id>`で記録し、CLIが`closed_at`を設定します。遷移配列や別の監査metadataは追加しません。判断が変わった理由は対象のthesis、holding review、ledgerなどの正本へ書き、taskにはcurrent stateと参照だけを残します。

## Task の内容

`body_md` には次だけを短く記載します。

- load-bearing question: 何を確認できれば判断が変わるか。
- primary sources: company IR、TDnet、EDINET、JPX、人間確認済み ledger など。
- expected destination: thesis、holding review、ledger などの更新先。
- close condition: task を `done` または `dropped` にできる条件。

`related_refs` には関連 record の repository 相対 path または出典 URL を置きます。逐次の作業ログや進行経緯は task record に複製しません。

## Resume checkpoint

resume時は次のqueryでopen taskをdue date順に確認し、各taskをcurrent canonical entityとledgerに照合します。

```bash
uv run baibai-engine task list --status open
```

1. `due_date` / `event_date`、ticker、load-bearing question、expected destination を読む。
2. current thesis、holding review、portfolio ledger、人間が報告した broker status と照合する。
3. 入力が一致し、trigger が到来した task を一件選ぶ。一次 IR の公表日程は実行時に再確認する。
4. 必要な canonical record を更新・検証し、人間確認後に task の current state を更新する。

task、canonical record、ledger の対象、数量、注文状態、判断、更新先が矛盾する場合は停止します。矛盾している入力、判断への影響、解除に必要な人間入力を確認し、推定で task や ledger を終端状態へ進めません。

broker 状態は人間の報告だけを事実入力とします。期日経過や reservation の不在だけから `filled / cancelled / expired` を推定しません。

## Validation

追加・変更はCLIから行います。入力契約、calendar date、enum、task_id一意性はwrite transactionより前に検証されます。

```bash
uv run baibai-engine task add --title '<title>' --kind <kind> --due YYYY-MM-DD
uv run baibai-engine task edit <task_id> --due YYYY-MM-DD
```
