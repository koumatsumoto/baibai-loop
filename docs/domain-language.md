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

<a id="analysis-domains"></a>

## 三つの分析領域

| 領域 | 中心となる問い |
| --- | --- |
| Valuation / 独自価値評価 | 自分たちの前提で、この株にどれだけの価値・収益を見込むか |
| Market Context / 市場環境 | 経済、市場全体、業種にどのような力が働いているか |
| External Assessment / 外部参考評価 | 外部の媒体・評価者はこの株をどう評価して提示しているか |

領域はL1観測・L2機械分析・L3 AI解釈という処理層と別の軸である。外部目標値は媒体が提示した値の観測であり、独自の企業価値認定ではない。Macro Contextは既存の判断成果物で、Market Context全体と同義ではない。現行配置は[architecture](./architecture.md#repository-map)を参照する。

Valuationの機械ER/FV anchorは倍率・自己価格レンジとcarryの推計であり、企業を調査したBase/DownsideのProjectionとは別結果である。AIが置いた前提を算術で計算しても、その前提や結果は観測事実にならない。4 Approachは候補探索の視点であり、四つの完成した企業価値モデルではない。

外部参考評価の提示値は「その媒体がその値を提示した」というL1観測であり、独自の企業価値や実際の大衆売買を認定した事実ではない。L2は出所ごとの受領比較と被覆・欠測を返す。同じ原評価元の転載は関係を示し、モデル価格、アナリスト予測/rating、Quants scoreを同じ多数決点へ変換しない。`retail_dynamics`は既存L1の物理identifierで、分析領域名や個人投資家の売買計測を意味しない。

## Canonical terms

| term | 種別 | 意味 |
| --- | --- | --- |
| Candidate | pipeline role / state | 調査・投資候補。具体artifactが分かる箇所では具体名を優先する |
| Candidate Discovery | activity | Security Analysis群からValuation ApproachごとのNominationを作りReview Setを形成する |
| Candidate Discovery Method | method | approach集合、common eligibility、Nomination union ruleを束ねるversioned method identity |
| Valuation Approach | method component | Valuation内で価値機会を発見する探索観点 |
| External Assessment | source-bound context | 外部媒体の提示値と出所別の機械比較。企業の独自FV・採否判断と別結果 |
| Machine Valuation | rebuildable estimate | 保存済みSecurity Analysisの機械ER・業種中央値/自己レンジanchor |
| Projection | conditional valuation | 調査で置いた期間内累積分配と分配後残余価値。Thesisが内包する |
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
| Market Snapshot | rebuildable state | benchmarkの週次方向、観測日を揃えたbreadthと業種中央値 |
| Security Market Context | rebuildable state | 指定銘柄の価格状態と同一端点での対benchmark/同業相対値 |
| Macro Reading | rebuildable artifact | Market Contextで参照する、経済系列からのdeterministic machine reading |
| Macro Context | artifact / judgment | Market Context内の既存環境判断成果物。領域全体とは区別 |
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

保存済み価格・master -> Market Snapshot / Security Market Context
外部媒体の提示値 -> External Assessmentの出所別比較・欠測 -> 判断の参考
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

内部の旧名・alias・adapterは原則残さず、caller・model・serializer・CLI・test・active docsを同じ変更で揃える。provider-native fieldと、保存済みjudgmentのidentity・引用・実取引事実は内部互換性と区別し、命名整理で書き換えない。
