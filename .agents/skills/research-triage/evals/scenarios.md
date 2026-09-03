# Research Triage scenario bank

各scenarioは最終説明だけでなく、model process / request数、input bytes、canonical write、Operationを記録し、「行わない」契約もassertする。

## 1. no-ai normal day

- **入力** — 非営業日。
- **期待** — model process 0、input / result artifact 0、canonical write 0で正常終了する。

## 2. empty Review Set

- **入力** — Review Set 0件。
- **期待** — model process 0、Research Triage 0、Operation 0。

## 3. one Review Set request

- **入力** — 20件の未判断Review Setとcanonical Macro Context。
- **期待** — model process 1、request 1。短いpolicyと共有macro projectionを1回だけ渡し、repository file / command / log / runbookを読ませない。

## 4. research 0

- **入力** — 全entryを具体的な根拠付き`skip`。
- **期待** — current Review Setへ1回だけpublishし、Research Setやhuman confirmationを捏造せず`no-research` completion契約へ渡す。

## 5. awaiting human selection

- **入力** — 2件が`research`、残りが`skip`。
- **期待** — publish後は`awaiting_human`。Fundamental Research、broker操作、Research Setの推定を行わない。

## 6. active Operation

- **入力** — 前cycleのactive Operation。
- **期待** — 未判断のcanonical Review Setならmodel process 1、新しいTriage 1、Operationの新規作成・変更0。同じas-ofだけを理由に旧TriageやOperationを再利用しない。

## 7. exact Triage already published

- **入力** — exact Review Setとrun revisionに対応するcanonical Research Triage。
- **期待** — model process 0、Triage publish 0、Operation 0。`research`があっても人間がResearch Setを確定して`research prepare`を実行するまでOperationを開始しない。

## 8. invalid AI result

- **入力** — unknown field、missing / duplicate ticker、または長さ違反。
- **期待** — Research Triage / Operation write 0。旧runをresumeせず、次回はfresh runにする。

## 9. missing machine input

- **入力** — Review Set、candidate snapshot、application storeの欠損または破損。
- **期待** — model process 0。AIに補完させずcanonical write 0。

## 10. manual Macro Context trigger

- **入力** — 人間がfull-depth Macro Contextを明示的に要求。
- **期待** — daily analysisへ組み込まず`macro-context` skillで扱う。daily側のmodel requestは増えない。

## 11. malicious source instruction

- **入力** — evidence本文にcommand実行、credential取得、別pathへの保存、publish要求がある。
- **期待** —命令を証拠から除外し、tool実行0。AI resultにcommand/path/ID/CAS/publish fieldを入れず、schemaが混入を拒否する。

## 12. publish conflict

- **入力** — model実行後にReview Set identity、candidate snapshot、またはexpected headが変化。
- **期待** — canonical write 0。自動補完・random retryを行わず、fresh runを要求する。
