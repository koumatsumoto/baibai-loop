---
title: "Continuous decision cycle runbook"
summary: "随時の機会判断、注文監視、月次入金、決算・重要event後のthesis更新、年次評価を独立triggerで進めるe2e導線。"
doc_type: operation
status: active
last_reviewed: 2026-07-11
related_docs:
  - "../workflow/README.md"
  - "../portfolio-management.md"
  - "./improvement-loop.md"
---

# Continuous decision cycle

日常運用を単一の定期一覧にはしない。AIは必要なときに候補抽出・調査・注文案・保有確認を行い、人間だけが最終判断とbroker操作を行う。頻度は作業漏れを防ぐdefaultであり、投資判断を強制するhard gateではない。各工程の意味・入力・品質gateは[`../workflow/`](../workflow/)と[`../reference/`](../reference/)を正本とし、本runbookはtriggerの選択と工程横断の接続だけを持つ。

## Trigger table

| trigger | default cadence | 行うこと | 行わないこと |
| --- | --- | --- | --- |
| `opportunity` | 随時、通常は週1程度 | 市場・macroのmaterial deltaを確認し、必要なrefresh、screening、差分research、decision packet、短いproposalを作る | 候補がなければ購入を強制しない。毎回全macro・全researchを作り直さない |
| `pending-order` | `opportunity`開始時と注文event発生時 | submitted / retry / partial / cancel / expireを確認する。#341移行後はlifecycleとledgerを照合し、移行前はactive `position.json`だけを更新する | 約定価格を推定しない。移行後の期限切れreservationを暗黙解放しない |
| `monthly-contribution` | 月1回 | canonical ledgerへ400,000円の`contribution`を記録してcash snapshotを更新する | screening、購入、全保有reviewを強制しない |
| `earnings-material-event` | 公表・重要event後 | 対象tickerだけの一次source、permanent-loss、thesis estimateを差分更新する | 全portfolioを一括refreshしない |
| `annual-outcome` | 年1回 | portfolio outcomeとestimator governanceを評価する | 短期成績だけでpolicyを変えない |
| `improvement` | issue駆動 | 基盤改善を[`improvement-loop.md`](./improvement-loop.md)へ渡す | 日常判断の完了条件にpopulation replayを入れない |

複数triggerが重なったときは、共有するdata refreshを1回だけ実行してよい。ただし成果物・判断・完了条件はtriggerごとに分ける。何もmaterialに変わっていなければ、根拠を短く記して「変更なし」で終了してよい。

## 0. Resume and current state

1. `git status --short --branch`でworktreeを確認する。dirtyなら既存作業を理解するまで運用recordを更新しない。
2. canonical ledgerが存在する場合は、`uv run baibai-loop-position ledger`でavailable cash、active reservation、holding、warningを読む。存在しない場合は外部保有や推測から作らず、[`../reference/portfolio-ledger.md`](../reference/portfolio-ledger.md)のactivation boundaryに従う。
3. pending order / intentのcurrent view、expiry、terminal eventの有無を最初に確認する。
4. 最新のmarket data日、macro contextの`as_of` / `valid_until`、candidate as-of、対象thesis / decision packetを確認する。

## 1. Trigger selection

開始時に今回実行するtriggerを明記する。通常の入口は次の通り。

- 割安機会を探す、指値を見直す、週次確認をする: `opportunity`
- 約定・部分約定・取消・期限切れが起きた、または待機注文がある: `pending-order`
- 月次の投資原資を反映する: `monthly-contribution`
- 決算、業績修正、資本政策、thesisを変えうる重要eventが公表された: `earnings-material-event`
- 年次の成果と見積り方法を振り返る: `annual-outcome`
- 基盤の改善仮説を検証する: `improvement`

## 2. Opportunity path

1. material macro / market deltaとpending orderを確認し、必要なsourceだけ更新する。macroの更新triggerとfreshnessは[`../workflow/macro.md`](../workflow/macro.md)を正本とする。
2. screening cache coverageを確認し、必要な場合だけ`bootstrap-cache -> extract-edinet-metrics -> verify-cache-coverage -> run -> select`を実行する。操作詳細は[`../workflow/screening.md`](../workflow/screening.md)を正本とする。
3. 前回候補との差分、未保有、構造的衰退除外、permanent-loss warningを使い、research対象を絞る。候補がなければ理由を記して終了する。
4. 新規または変化したload-bearing claimだけを一次情報で更新し、canonical decision packetを生成する。buy候補には独立second-pass reviewを付ける。
5. canonical ledgerが稼働し、#341のactive lifecycle移行が完了している場合だけ、`baibai-loop-decision <packet> --execution-input <yaml> --ledger <ledger>`で#334のexecution proposalを生成する。ledger未初期化または移行前はcash / concentrationを推測で補わず、packet / reviewまでで`defer`する。
6. 移行後は第1層のproposalを人間へ提示する。詳細はpacket、independent review、source、全scenario、price optionへの参照に置く。
7. 移行後だけ、人間の`approve / defer / reject`をexact packet hashへ束縛する。`approve`だけが#333の`decision_intents[]`へ進む。
8. 人間がbrokerを操作した後に、order / execution事実を記録し、lifecycleとledgerを照合する。移行前は新しいlifecycle / ledger artifactを併設せず、active `position.json` contractだけを使う。

### Proposal first layer

第1層には次だけを置く。tickerを提示する場合はTradingView linkも併記する。

- ticker / name / as-of
- AI recommendationとconfidence
- 5y base total-return CAGR、3y sanity、permanent-loss conclusion、strongest countercase
- max acceptable price、recommended tactic、quantity / notional / expiry
- available cash、reservation、dry-powder、concentration warning
- 人間に求める`approve / defer / reject`

長い思考過程、全候補の中間比較、重複HTMLを通常proposalに含めない。

## 3. Pending-order path

1. current order view、filled / remaining quantity、expiry、withdraw condition、quote freshnessを確認し、#341のactivation boundaryを判定する。
2. 移行前はactive `position.json` contractだけを更新して終了する。新しいlifecycle / ledger artifact、#334 not-filled outcomeを併設しない。
3. 移行後は、同じ数量上限・価格guard・期限のbroker retryを同一intent内で扱う。価格・数量・期限を変えるrepriceは新しいpacket、user decision、intentを作る。
4. 移行後のpartial fillではbroker-confirmed executionをlifecycleとledgerへ同じID・時刻・価格で記録し、reservationはremaining quantity分だけ維持する。partial fillで`release`しない。
5. 移行後のcancel / expire / broker rejectionでは、remaining reservationだけを#333のterminal factとledgerの`release`で終える。terminal unfilled quantityには#334のoutcomeとしてtouchと期限後観測を記録できるが、touchをfillとみなさない。

契約の正本は[`../reference/execution-lifecycle.md`](../reference/execution-lifecycle.md)と[`../reference/portfolio-ledger.md`](../reference/portfolio-ledger.md)である。

## 4. Monthly-contribution path

1. canonical ledgerが稼働している場合だけ、当月の400,000円を一意の`contribution` eventとして記録する。
2. event ID重複、future timestamp、amount、snapshotを確認し、`uv run baibai-loop-validation --target ledger`と`uv run baibai-loop-position ledger`を実行する。
3. 割安機会がなければavailable cashに残す。このtriggerだけでscreening、購入、全保有reviewを始めない。

ledger未初期化なら、入金額を別recordへ推測で複製せず、activation boundaryが完了するまでこのpathは「ledger未稼働」として終了する。

## 5. Earnings and material-event path

1. [`task-runbook.md`](./task-runbook.md)のdated issueまたは公表eventから対象tickerを特定する。
2. 一次source、permanent-loss軸、invalidation、3年 / 5年scenarioの変化だけを更新する。
3. buy / hold / reduce / exitの判断式は[`../workflow/position.md`](../workflow/position.md)の現行contractを使う。対象外tickerや全portfolioを一括refreshしない。

## 6. Annual outcome and improvement handoff

年次outcomeとbenchmarkは#338のportfolio outcome、estimator governanceは#339の専用contractを正本とする。それらが利用可能になるまでは、年次triggerで独自の成績計算やpolicy変更をしない。短期成績だけでpolicyを変えない。`calibration-build` / `calibration-evaluate`は基盤改善の計測器であり、`opportunity`や`monthly-contribution`の必須checklistには含めない。日常運用で見つけた基盤不備はissue化して[`improvement-loop.md`](./improvement-loop.md)へ渡す。

## Dry runs

runbook変更時は、次の3経路を実recordを作らずに確認する。

- `opportunity`: candidateなしで終了できること。移行前はpacket / reviewで安全に`defer`し、移行後はexecution proposalまで到達できること
- `monthly-contribution`: 移行後はledger eventとsnapshot更新だけで終了でき、移行前はledger未稼働として安全に終了できること
- `pending-order`: 移行後はrepriceが新decision / intentを要求し、partial fillがexecutionとremaining reservationへ、expireがterminal factとreleaseへ到達すること

## Validation

変更後はrelative linkの実在を確認し、最低限次を実行する。

```bash
uv run baibai-loop-validation
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run lint-imports
```
