---
title: "タスク runbook"
summary: "GitHub issue で決算後確認などの運用タスクを管理するための入口。"
doc_type: operation
status: active
last_reviewed: 2026-07-14
related_docs:
  - "../workflow/research.md"
  - "../workflow/position.md"
---

# タスク runbook

GitHub issue は、決算後確認など「将来の特定イベント後に実行する作業」の運用タスク一覧として使います。triggerの選択と全体導線は[`decision-cycle.md`](./decision-cycle.md)を正本とし、投資判断の正本はrecordsに残します。issueには実行漏れを防ぐための期限、確認項目、更新先を記録します。

## 対象

次のいずれかに該当する場合、タスク issue を作成または既存タスク issue に紐づけます。

- `judgment.recommendation: defer` のdecision packetで、再確認すべきイベントを特定した。
- 既存保有に、決算発表後の holding review 更新がある。
- screening / research の途中で「YYYY-MM-DD の決算後に確認」のような実行日付きの判断待ちが発生した。

単なる調査メモ、将来いつか確認する改善案、screening rule 改訂案はこの runbook の対象外です。別途 follow-up issue として扱います。

## 粒度

- 同じ期限日、同じタスク種別、同じ領域の未保有候補は、原則 1 issue にまとめます。
- 既存保有、売買判断が絡むもの、高重要候補、確認項目が大きく異なるものは個別 issue にします。
- 既に同じ期限日のタスク issue がある場合は、新規作成前にその issue へ追記できるか確認します。
- 重複 issue を見つけた場合は、残す issue に内容を移し、重複側に移管先をコメントして close します。

<a id="open-issue-lifecycle"></a>

## Open dated Issue の lifecycle

resume時は、期限、注文、次eventのいずれかを持つopenなdated trade/task Issueを一覧し、各Issueのcurrent load-bearing question、expected destination、close conditionをcanonical recordsとledgerへ照合します。未完了の人間確認や将来triggerを失わず、Issueだけに残った判断をcanonical stateと誤認しないための監査です。

照合対象は次のとおりです。

- due event/date、ticker、current question、関連proposal/packet/holding review、expected destination
- canonical decision packet、holding review、portfolio ledgerのcurrent stateとsource/hash
- broker操作が関係する場合、人間が報告した`open / filled / cancelled / expired`と各statusの必須情報

Issue、canonical record、ledgerの間で対象、数量、注文状態、判断、更新先のいずれかが矛盾する場合はstopします。矛盾している入力、判断への影響、解除に必要な人間入力をIssueへコメントし、records更新、ledger更新、Issue closeを推定で進めません。

broker状態は人間の報告だけを事実入力とします。期日を過ぎたこと、ledgerにreservationがないこと、proposalの判断が現在と合わないことだけから`filled / cancelled / expired`を推定しません。人間のstatus報告がないIssueでは注文状態を変えず、照合結果と必要な報告をコメントします。

監査後はIssueを次のいずれかとして扱います。

- **`complete`**: load-bearing questionへの判断が確定し、必要なcanonical recordまたはledger更新が完了している。更新不要ならcurrent canonical stateで解決できることを確認し、current decisionとその理由をコメントしてcloseします。該当canonical recordが存在する場合はpath/hashも示します。
- **`current-question-invalid`**: Issueのload-bearing questionがcurrent canonical stateでは成立しない。current canonical stateまたは判断根拠への参照と、そのquestionを維持しない理由をコメントしてcloseします。該当canonical recordが存在する場合はpath/hashも示します。別のtriggerや判断が残る場合は既存Issueへ移すか新しいIssueに分割し、移管先と理由をコメントします。
- **`living`**: 次に判断できるdated triggerまたは人間入力が未到来である。次のtrigger/date、openのまま残す理由、その時点で必要な一次sourceまたは人間入力をコメントしてopenを維持します。
- **`blocked`**: canonical inputsに矛盾または不足がある。該当入力、判断への影響、解除に必要な人間入力をコメントしてopenを維持します。

close、分割、継続のコメントは現在の正本、現在の判断、その判断を支える理由を示します。Issue本文を判断の正本にせず、投資判断と注文状態はそれぞれcanonical recordsと人間確認済みledgerに残します。

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
## Trigger

- due event / date:
- ticker / name:
- related proposal / packet / holding review:

## Load-bearing question

- このeventで何が確認できれば判断が変わるか:
- 現在blocked/deferの理由:

## Primary sources

- company IR / TDnet / EDINET / JPX:
- 対象期、公表予定日:

## Expected destination

- decision packet / holding review / ledger（必要なものだけ）:

## Close condition

- current lifecycle: living / complete / current-question-invalid / blocked
- complete時のcurrent canonical state、判断、該当recordがある場合はpath/hash:
- current-question-invalid時のcurrent canonical stateまたは判断根拠、close理由、該当recordがある場合はpath/hash、必要なら移管先:
- living時のnext dated trigger/dateとopenのまま残す理由:
- blocked時の矛盾・不足inputと解除条件:
```

## 完了時

resume時の監査とIssueのclose/分割/継続判定は[`Open dated Issue の lifecycle`](#open-issue-lifecycle)に従います。

1. 会社の一次 IR を確認する。
2. issue の確認項目に沿って判断を決める。
3. 必要なdecision packet、holding review、ledgerだけを更新する。
4. issueにcurrent canonical stateと1〜3行の判断結果をコメントする。canonical recordを更新または参照した場合はpath/hashも示し、判断本文を複製しない。
5. タスクが完了したら issue を close する。

canonical recordを更新する判断はrecordsへ戻し、recordを作らない正常終了はcurrent canonical stateと判断根拠への参照をissueへ残す。issue本文だけに新しいcanonical判断を作らない。
