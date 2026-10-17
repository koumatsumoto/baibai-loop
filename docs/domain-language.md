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

情報の生成責務はL1 Collectionの外部入力保持、L2の決定的な解析、L3 Researchの統合・解釈・判断に分ける。上の三領域は主にL2の分析結果を整理する区分で、それぞれが独立した三層一式を持つ意味ではない。Market Context AssessmentはResearchが所有するL3判断成果物で、領域全体と同義ではない。Readingは登録指標系列を機械的に読むL2であり、Market SnapshotやSecurity Market Contextとは別の結果である。境界・保存責務は[architecture](./architecture.md)が所有する。

Valuationの機械ER/FV anchorは倍率・自己価格レンジとcarryの推計であり、企業を調査したBase/DownsideのProjectionとは別結果である。AIが置いた前提を固定すると算術は決定的に再現できるが、前提は観測にならず、結果は条件付き見積りである。4 Approachは候補探索の視点であり、四つの完成した企業価値モデルではない。

外部アナリストの判断も「その媒体がそう提示した」受領資料としてL1に保持し、独自の企業価値や実際の大衆行動を測定した事実とは扱わない。HTMLやMCP応答からの値の抽出・正規化、出所ごとの比較と被覆・欠測はL2であり、型付き観測を受領原本そのものとは呼ばない。自分たちの解釈はL3に置く。同じ原評価元の転載は関係を示し、尺度・期間の異なるモデル価格、アナリスト予測/rating、Quants scoreを単一投票へ潰さない。媒体のQuants総合scoreは媒体値であり、独自総合scoreや自動注文は作らない。

## Canonical terms

| term | 種別 | 意味 |
| --- | --- | --- |
| Observation | evidence value | 受領原本と機械的に整理した観測を区別する。型名だけではL1と決めず、source契約・provenanceで確認する |
| Assumption | judgment input | AI/人間が置く前提。観測と区別する |
| Conditional Estimate | estimate | 観測と仮定/規則から導出する条件付き見積り |
| Candidate | pipeline role / state | 調査・投資候補。具体artifactが分かる箇所では具体名を優先する |
| Candidate Discovery | activity | 同runのStock Analysis群にValuation Approachごとの候補理由を確定する |
| Candidate Discovery Method | method | approach集合、common eligibility、順位とunion ruleを束ねるversioned method identity |
| Valuation Approach | method component | Valuation内で価値機会を発見する探索観点 |
| External Assessment | L2 analysis | 外部媒体の受領原本から抽出した提示値と出所別の機械比較。企業の独自FV・採否判断と別結果 |
| Machine Valuation | rebuildable estimate | 保存済みStock Analysisの機械ER・業種中央値/自己レンジanchor |
| Projection | conditional valuation | 調査で置いた期間内累積分配と分配後残余価値。Stock Assessmentが内包する |
| Screening Run | artifact | ある`as_of`に対するscreening一回分 |
| Stock Analysis | artifact entry | 一銘柄のobserved / derived / estimateと、同runで確定する候補文脈 |
| CandidateReason | value | 候補になったValuation Approach / MethodとそのApproach内のrank |
| StockCandidateContext | value | common eligibility、候補理由、候補母集団内のnormalized gap |
| 候補一覧 | read projection | 同runの`candidate.reasons`が空でないStock Analysisのticker昇順投影 |
| Research Set | pipeline state | 明示開始時に固定する新規または保有の有限対象。[三入口のResearch選定](./reference/analysis-operations.md#明示開始と固定対象)で選び、保存capacityに従い0件も扱う |
| Stock Assessment | artifact / judgment | 一銘柄の投資仮説、valuation、risk、evidence |
| Assessment Review | artifact / judgment | Stock Assessmentへの独立second pass |
| Investment Proposal | artifact / judgment | 評価を価格・cash・保有に適用した行動と保存条件、全対象の比較 |
| Limit Terms | proposal value | 提案に保存するprice / quantity / session / conditions |
| broker fact | human-confirmed fact | 人間がbroker画面等から確認して報告したorder / fill fact |
| Portfolio Ledger | artifact | human-confirmed event ledger |
| Reservation | ledger state / event | 買付資金または売付可能な保有数量を拘束する注文状態・event |
| Execution | business fact | 約定事実 |
| Holding | derived state | Ledger replayから得る保有状態 |
| Position Review | activity | Researchの保有scopeでhold / sell / deferの提案を作る見直し |
| Portfolio Outcome | artifact | portfolio-wide TWR / benchmark comparison publication |
| Market Snapshot | rebuildable state | benchmarkの週次方向、観測日を揃えたbreadthと業種中央値 |
| Security Market Context | rebuildable state | 指定銘柄の価格状態と同一端点での対benchmark/同業相対値 |
| Reading | rebuildable state | Market Contextの登録指標系列から得る決定的な記述統計・鮮度・変化。独立DB/判断成果物ではない |
| Market Context Assessment | artifact / judgment | Researchが所有し、明示開始で作る重要変化・根拠・反証・不確実性・調査/リスクへの含意 |
| E[r] Calibration Context | rebuildable context | Screeningが生成し、Valuationの契約・解釈でResearch等が読む過去較正のL2 current context |
| Official Benchmark Observation | observation | Market Contextが意味契約を所有する公式TOPIX gross-total-return観測。price-only市場方向評価とは別 |
| Operation Session | application state | 明示開始に束縛した有限調査と成果の状態。L3完了と人間採否・取引事実を区別 |

「4 Valuation Approach」はcurrent production methodの構成であり、候補一覧のdomain定義ではない。
current methodのapproach数と候補選抜depthは
[`screening-runtime.md`](./reference/screening-runtime.md)が記述する。

## Artifact、activity、state、method、physical store

artifactは保存または受け渡す構造化成果物、activityはartifactを作る処理、pipeline stateは
人間またはsystemが次工程へ進める対象の状態、methodはversionedな判断・計算規則である。
physical storeはこれらの格納場所であり、domain termの意味を所有しない。たとえば
Candidate Discoveryはactivity、Candidate Discovery Methodはmethod、候補一覧は同runのread projection、
Research Setはpipeline stateである。storeごとのauthorityは
[`architecture.md#store-authority`](./architecture.md#store-authority)を参照する。

## Decision flow

```text
Collectionの保存L1 -> 共通market L2 -> Valuation / Market Context等のL2結果
             -> Screening Run -> Stock Analysis[] + 同runの候補文脈
             -> candidate.reasonsがある同じStock Analysisの候補一覧
Stock Analysis / 候補一覧 -> Research selection (新規探索)
指定銘柄 -> Research selection (候補外も可)
Holding -> Research selection (保有見直し)
各入口 -> 明示開始・単一scopeと保存capacityによる固定選択
           -> 保存済みL2結果と根拠資料による有限Research -> Stock Assessment + Assessment Review
reviewed Stock Assessment群 + 現在価格/cash/保有 -> Investment Proposal
                 -> buy / hold / sell / no_action / defer + 条件付きterms
                 -> 人間の採否・broker操作・報告 -> Portfolio Ledger -> Holding
Holding -> 同じ有限Researchの保有scope -> Stock Assessment + Assessment Review
        -> 仮説維持と当日算術を分けたInvestment Proposal
Portfolio Ledger -> Portfolio Outcome
実現結果 -> Calibration -> 次のmethod改善

保存済み価格・master -> Market Snapshot / Security Market Context
外部媒体の提示値 -> External Assessmentの出所別比較・欠測 -> 判断の参考
登録指標の保存観測 -> Reading（機械的な統計・鮮度・変化）
保存済み観測・Reading・市場/個別状態・一次資料
       -> 明示作成するMarket Context Assessment
Market Context Assessment -> Research・資本判断の補助入力
```

新規探索・指定銘柄・保有見直しの三入口を同じResearchで選定し、scopeごとに有限対象を固定する。保有は現在ledgerと照合し、候補所属を要求しない。新規と保有の行動は共通のInvestment Proposalへ保存し、評価と行動、保存条件と現在previewを分ける。Proposal公開・Operation完了を人間の採用や確認済み注文・約定へ変換しない。

これは成果物の関係であり、毎回すべてを実行する手順ではない。0件・部分完了・未完了は調査進行の結果として残し、AI完了を人間の採用や取引事実と混同しない。操作ごとの開始条件と人間確認は[各skill](../.agents/skills/)を参照する。

Readingの現行範囲は`market_context/indicators`のregistryに登録された系列で、経済統計・金利・信用/流動性・為替/商品・株価指数やvolatility等を含む。`market_context/reading`がdated規則で統計と鮮度を計算し、市場全体/業種のbreadthや指定銘柄の相対状態は別の`market_context/snapshot / security`が所有する。全Market ContextのL2を一つのReadingへ統合したという意味ではない。現行canonical名はReadingとし、旧資料のMacro Readingはこの指標読取の歴史的呼称として読む。

Market Context Assessmentの実モデルは`research/market_context/models.py`の`MarketContextAssessment`である。旧Macro Contextのschema3/4は保存された章立て判断の歴史的用語、schema5/6は同じコンパクト判断の意味を持つ。`macro-context-*`等の保存identityや引用は文字列のまま保全し、名称やID prefixだけで内容を旧章立てへ分類しない。Readingは判断を自動生成せず、L3の明示作成と履歴読取を分ける。

## Naming grammar

- `Candidate`はpipeline role / stateを指す場合だけ使う。Stock Analysis、候補一覧 Entry、
  Stock Assessmentのrevisionを指すsurfaceでは具体名を使う。
- `candidate_discovery_method`と`CandidateDiscoveryRules`はcanonical namingである。
  identity objectは`CandidateDiscoveryMethodIdentity`、persisted fieldは
  `candidate_discovery_method_id`、identity digestは`method_hash`とする。
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
