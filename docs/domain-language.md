---
title: "Domain language"
summary: "Baibai Loop のdomain term、artifact/state/activity/methodの区別、命名文法の正本。"
doc_type: reference
status: active
---

# Domain language

この文書はdomain termとterm同士の関係を所有する。目的と判断原則は
[`doctrine.md`](./doctrine.md)、layer・dependency・physical authorityは
[`architecture.md`](./architecture.md)、artifact固有のfield・式・warningは
[`reference/`](./reference/)、operation手順は[各skill](../.agents/skills/)を正本とする。

## Canonical terms

| term | 種別 | 意味 |
| --- | --- | --- |
| Candidate | pipeline role / state | 調査・投資候補。具体artifactが分かる箇所では具体名を優先する |
| Candidate Discovery | activity | Security Analysis群からValuation ApproachごとのNominationを作りReview Setを形成する |
| Candidate Discovery Method | method | approach集合、common eligibility、Nomination union ruleを束ねるversioned method identity |
| Valuation Approach | method component | 価値機会を見る独立した探索観点 |
| Screening Run | artifact | ある`as_of`に対するscreening一回分 |
| Security Analysis | artifact entry | 一銘柄についてscreeningが作るobserved / derived / estimateの比較単位 |
| Nomination | fact | Valuation ApproachがReview Set入りを推薦した事実 |
| Review Set | artifact | configured Valuation Approachesが生成したNominationのexact union |
| Review Set Entry | artifact entry | Review Set内の一銘柄と判断時機械座標 |
| Research Triage | artifact / judgment | Review Set全件を`research / skip`に分類するcanonical L3 judgment |
| Research Set | pipeline state | Triageの`research` entryから人間が実際にResearchへ進める集合。独立persisted artifactではない |
| Thesis | artifact / judgment | 一銘柄の投資仮説、valuation、risk、evidence |
| Thesis Review | artifact / judgment | Thesisへの独立second pass |
| Capital Allocation Assessment | artifact / judgment | reviewed Thesis群をportfolio全体に照らした配分判断 |
| Planning Limit | ephemeral input | broker操作前のprice / quantity / expiry guard |
| broker fact | human-confirmed fact | 人間がbroker画面等から確認して報告したorder / fill fact |
| Portfolio Ledger | artifact | human-confirmed event ledger |
| Reservation | ledger state / event | 買付余力を拘束する状態またはevent |
| Execution | business fact | 約定事実 |
| Holding | derived state | Ledger replayから得る保有状態 |
| Position Review | artifact / judgment | Holdingの`hold / exit / null`再評価 |
| Portfolio Outcome | artifact | portfolio-wide TWR / benchmark comparison publication |
| Macro Reading | rebuildable artifact | provider factからのdeterministic machine reading |
| Macro Context | artifact / judgment | 現在のmacro judgment |
| E[r] Calibration Context | rebuildable context | 過去較正をResearch Triage等が読むL2 current context |
| Operation Session | application state | human gateをまたぐworkflow session |

「4 Valuation Approach」はcurrent production methodの構成であり、Review Setのdomain定義ではない。
current methodのapproach数とNomination depthは
[`screening-runtime.md`](./reference/screening-runtime.md)が記述する。

## Artifact、activity、state、method、physical store

artifactは保存または受け渡す構造化成果物、activityはartifactを作る処理、pipeline stateは
人間またはsystemが次工程へ進める対象の状態、methodはversionedな判断・計算規則である。
physical storeはこれらの格納場所であり、domain termの意味を所有しない。たとえば
Candidate Discoveryはactivity、Candidate Discovery Methodはmethod、Review Setはartifact、
Research Setはpipeline stateである。storeごとのauthorityは
[`architecture.md#store-authority`](./architecture.md#store-authority)を参照する。

## Decision flow

```text
Screening Run -> Security Analysis[]
             -> Valuation Approach[] -> Nomination[] -> Review Set
Review Set -> Research Triage
           -> 人間のResearch Set選択 -> Research -> Thesis + Thesis Review
           -> Capital Allocation Assessment -> Planning Limit
           -> 人間のbroker操作・報告 -> Portfolio Ledger -> Holding
Holding -> Position Review
Portfolio Ledger -> Portfolio Outcome
実現結果 -> Calibration -> 次のmethod改善

provider facts -> Macro Reading -> Macro Context
Macro Context -> Triage・Research・資本判断の補助入力
```

これは成果物の関係であり、毎回すべてを実行する手順ではない。操作ごとの開始条件と人間確認は[各skill](../.agents/skills/)を参照する。

## Naming grammar

- `Candidate`はpipeline role / stateを指す場合だけ使う。Security Analysis、Review Set Entry、
  replacement Thesisを指すsurfaceでは具体名を使う。
- `candidate_discovery_method`と`CandidateDiscoveryRules`はcanonical namingである。
  identity objectは`CandidateDiscoveryMethodIdentity`、persisted fieldは
  `candidate_discovery_method_id`、identity digestは`method_hash`、Triage契約は
  `triage_contract_id`とする。
- 人間報告のorder / fill factは`broker fact`と呼び、新しいBroker Report artifactは作らない。
- `result`、`context`、`row`、`item`、`payload`、`path`は一律renameしない。domain-facing
  surfaceで誤読する場合だけ具体化する。
- E[r]と`er_*`は定義済みの金融略語として維持する。
- provider-nativeな`Code`等はadapter boundaryに残し、in-memory domain objectでは
  `ticker`等を使う。technical scopeが明確な`GcCandidate`等は投資用語のCandidateではない。

## Time vocabulary

| name | meaning |
| --- | --- |
| `as_of` | logical cutoff |
| `observed_at` | source observation timestamp |
| `generated_at` | machine / projection generation timestamp |
| `published_at` | canonical publication timestamp |
| `occurred_at` | business event timestamp |

provider-native fieldとmigrationしないpersisted legacy keyはstorage contractとして維持できる。
adapterとread modelはcurrent domain nameへ変換し、legacy nameを新しいdomain-facing surfaceへ
伝播させない。
