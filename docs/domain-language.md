---
title: "Domain language"
summary: "Baibai Loop のdomain term、artifact/state/activity/methodの区別、命名文法の正本。"
doc_type: reference
status: active
---

# Domain language

割安銘柄の発見、割安幅と条件付き収益の評価、根拠付き売買提案に使う用語と成果物の関係を定義する。実売買は人間が行う。

この文書はdomain termとterm同士の関係を所有する。目的と判断原則は
[`doctrine.md`](./doctrine.md)、layer・dependency・physical authorityは
[`architecture.md`](./architecture.md)、実契約のfield・式・warningは
[`reference/`](./reference/)、operation手順は[各skill](../.agents/skills/)を正本とする。

<a id="analysis-domains"></a>

## 三つの分析領域

| 領域 | 中心となる問い |
| --- | --- |
| Valuation / 独自価値評価 | 自分たちの前提で、この株にどれだけの価値・収益を見込むか |
| Market Context / 市場環境 | 経済・金利等の環境と市場全体・業種・個別相対はどのような状態か |
| External Assessment / 外部参考評価 | 外部の媒体・評価者はこの株をどう評価して提示しているか |

領域は業務の横方向、L1観測・L2決定的な計算・L3 AI解釈/調査/提案は縦方向の区別である。Macro ContextはMarket Context内の既存判断成果物で、領域全体と同義ではない。境界・保存責務と移行状況は[architecture](./architecture.md)が所有する。

Valuationの機械ER/FV anchorは倍率・自己価格レンジとcarryの推計であり、企業を調査したBase/DownsideのProjectionとは別結果である。AIが置いた前提を固定すると算術は決定的に再現できるが、前提は観測にならず、結果は条件付き見積りである。4 Approachは候補探索の視点であり、四つの完成した企業価値モデルではない。

外部参考評価の提示値は「その媒体がその値を提示した」というL1観測であり、独自の企業価値や実際の大衆行動を測定した事実ではない。L2は出所ごとの受領比較と被覆・欠測を返す。同じ原評価元の転載は関係を示し、尺度・期間の異なるモデル価格、アナリスト予測/rating、Quants scoreを単一投票へ潰さない。媒体のQuants総合scoreは媒体値であり、三領域の独自総合scoreではない。`retail_dynamics`は既存L1の物理identifierで、分析領域名や個人投資家の売買計測を意味しない。

## Canonical terms

| term | 種別 | 意味 |
| --- | --- | --- |
| Observation | source fact | 原典の値・出所・時点・欠測を保持した観測 |
| Assumption | judgment input | AI/人間が置く前提。観測と区別する |
| Conditional Estimate | estimate | 観測と仮定/規則から導出する条件付き見積り |
| Candidate | pipeline role / state | 調査・投資候補。具体artifactが分かる箇所では具体名を優先する |
| Candidate Discovery | activity | Security Analysis群からValuation ApproachごとのNominationを作りReview Setを形成する |
| Candidate Discovery Method | method | approach集合、common eligibility、Nomination union ruleを束ねるversioned method identity |
| Valuation Approach | method component | Valuation内で価値機会を発見する探索観点 |
| External Assessment | source-bound context | 外部媒体の提示値と出所別の機械比較。企業の独自FV・採否判断と別結果 |
| Machine Valuation | rebuildable estimate | 保存済みSecurity Analysisの機械ER・業種中央値/自己レンジanchor |
| Projection | conditional valuation | 調査で置いた期間内累積分配と分配後残余価値。Thesisが内包する |
| Screening Run | artifact | ある`as_of`に対するscreening一回分 |
| Security Analysis | artifact entry | 一銘柄についてscreeningが作るobserved / derived / estimateの比較単位 |
| Nomination | selection record | Valuation ApproachがReview Set入りを推薦した事実 |
| Review Set | artifact | configured Valuation Approachesが生成したNominationのexact union |
| Review Set Entry | artifact entry | Review Set内の一銘柄と判断時機械座標 |
| Research Triage | artifact / judgment | ResearchがReview Set全件を`research / skip`に分類するcanonical L3 judgment |
| Research Set | pipeline state | 明示開始時に固定する新規調査集合。Triage admission内で保有調査と合算最大三対象、0件も扱う |
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
| Operation Session | application state | 明示開始に束縛した有限調査と成果の状態。L3完了と人間採否・取引事実を区別 |

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
三領域の保存済み観測・L2結果 -> Screening Run -> Security Analysis[]
             -> Valuation Approach[] -> Nomination[] -> Review Set
Review Set -> Research Triage
           -> 明示開始・新規/保有合算最大三対象の固定選択
           -> 有限Research -> Thesis + Thesis Review
reviewed Thesis群 -> Capital Allocation Assessment -> Planning Limit
                  -> 一銘柄ずつの新規買付/見送り提案
                  -> 人間の採用・broker操作・報告 -> Portfolio Ledger -> Holding
Holding -> 同じ有限選択の保有調査 -> Position Reviewのhold/exit/null提案
       -> 人間確認と入力再検証 -> 確認済みPosition Review
Portfolio Ledger -> Portfolio Outcome
実現結果 -> Calibration -> 次のmethod改善

保存済み価格・master -> Market Snapshot / Security Market Context
外部媒体の提示値 -> External Assessmentの出所別比較・欠測 -> 判断の参考
provider facts -> Macro Reading -> Macro Context
Macro Context -> Triage・Research・資本判断の補助入力
```

新規提案はCAA/Planning、保有提案はPosition Reviewの成果に含む。共通の新しいTrade Proposal永続artifactを追加したという意味ではない。

これは成果物の関係であり、毎回すべてを実行する手順ではない。0件・部分完了・未完了は調査進行の結果として残し、AI完了を人間の採用や取引事実と混同しない。操作ごとの開始条件と人間確認は[各skill](../.agents/skills/)を参照する。

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
| `as_of` | logical target/cutoff。判断の対象時点 |
| `observed_at` | source observation timestamp。原典が観測した時点 |
| `fetched_at` / `fetched_at_utc` | 原典を実際に取得・受領した時刻 |
| `generated_at` | machine / projection generation timestamp |
| `published_at` | canonical publication timestamp |
| `occurred_at` | business event timestamp |

対象・観測・取得・公開の時点は同じとは限らない。判断入力は値と出所、これらの時点、欠測/比較上の制約を持ち、原典が与えない時点や期間を推測で補わない。fieldごとの厳密な形はreferenceが所有する。

内部命名の変更と、保存済み判断identity・引用・取引事実の保持は[architectureの命名・履歴原則](./architecture.md#naming-and-history)に従う。
