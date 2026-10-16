---
title: "Architecture"
summary: "分析領域・依存・保存責務と、現行writer・保存履歴の境界。"
doc_type: architecture
status: active
---

# Architecture

Baibai Loopの目的は、割安銘柄を発見し、割安幅と条件付き収益を評価し、根拠を伴う一銘柄ずつの売買提案へつなげることである。実売買、提案の採用、注文・約定の確認は人間が行う。

この文書は設計上の境界・依存・保存責務を所有する。用語と成果物の関係は[domain-language](./domain-language.md)、実契約は[reference](./reference/README.md)、操作は[各skill](../.agents/skills/)と[batch運用](../batch/OPERATIONS.md)が所有する。Issueは意思決定と移行の証拠であり、現行仕様をコメントだけに置かない。以下に責務と現行境界を示す。

<a id="information-layers"></a>

## 1. 分析領域と情報層

中心ユースケースは、L1観測を使った決定的L2の日次機会発見を行うScreeningと、候補・三領域の結果・一次資料を用いてL3調査と売買提案を行うResearchである。[三分析領域](./domain-language.md#analysis-domains)は何を分析するか、Screening/Researchはそれらをどう使うかという横断業務を表す。Position/Portfolioは取引事実と資本を扱う周辺業務で、人間の採否・注文は外部工程である。情報の縦方向は次の層で区別し、領域ごとの所有は[下表](#repository-map)に示す。

| 層 | 内容 |
| --- | --- |
| L1 観測 | 原典から取得した値と、その出所・時点・被覆・欠測 |
| L2 機械分析 | 固定した入力と規則から再現できる決定的な計算・比較 |
| L3 AI | 解釈、仮定の設定、有限調査、判断、条件付き評価・提案 |

AIが置く仮定を決定的算術で計算しても、仮定は観測にならず、結果は条件付き見積りである。各領域でL1/L2/L3の責務を識別するが、九つの空package、三DB、三サービスを作る区分ではない。共有するL1の純粋モデルや小さな算術にも意味の所有者を保つ。

L1の保存構造は、提供元の原本・provider-native fieldと欠測の意味を可能な限り残すことを原則とする。仕様・実装を簡素化することと、元データを減らすことは分けて判断する。現在の計算が使わないという理由だけで値を捨てず、L2の再計算・分析の選択肢を残す。同じ取得で得られる値の保持を優先し、追加の取得量・保存量・利用条件に対して用途が乏しいものは対象から外す。型付け・日付形式・索引の整備で意味を変えず、元の観測を再解釈できる入力を残す。APIの原本JSONとCSVから組み立てたrecordは区別し、後者を原本API payloadと呼ばない。原本が失われた旧行を推測で補完し、原本を保持した形に見せない。

取引カレンダーは提供元の休日区分（HolDiv）を保持し、現物株の営業日判定は読取時に区分1・2から導く。区分0（非営業日）と3（祝日取引あり）、区分1（営業日）と2（半日立会日）を保存時に潰さない。旧booleanから原区分は復元せず、切替時に原本から再構築する。

期間集計、株式単位の換算、予想の選択、欠測の扱い、倍率・比較・見積りはL2の責務とし、加工済みの値だけをL1の正本にしない。検索や計算効率のための展開列・materializationは元入力から再生成できる副次構造とし、独立した二つの正本を作らない。既存schemaの維持や互換処理の追加より、現行ドメインに必要な保存構造への見直しを優先する。保管対象と期間は業務・再計算に必要な範囲とし、全要求の監査や一律の永久保存を目的にしない。

判断用入力は値に加え、出所、対象・観測・取得・公開の時点、欠測と比較上の制約を持つ。転載を独立票と数えず、異種のrating・目標株価・Quants scoreを単一投票に潰さない。三領域の総合scoreや自動注文を追加しない。

<a id="layers"></a>

## 2. 最終依存境界

```mermaid
flowchart LR
    web[Web] --> read[read_api]
    batch[Batch] --> api[batch_api]
    batch --> read
    research[Research] --> valuation[Valuation]
    research --> context[Market Context]
    research --> external[External Assessment]
    screening[Screening] --> valuation
    screening --> context
    read --> valuation
    read --> context
    read --> external
    valuation --> facts[共有L1の純粋モデル・算術]
    context --> facts
    external --> facts
    research --> position[Portfolio / Position]
```

図の矢印は業務入力・読取の依存を示す。三領域は相互やResearch・Screening・UIへ逆依存しない。Researchは三領域の公開読取入口を呼び、UI向け`read_api`を内部業務入力にしない。現行Screening RunはValuationとMarket Contextの結果を組み立て、母集団・候補探索・候補一覧を所有する。External AssessmentはResearchと公開読取の参考入力であり、候補一覧の推薦・所属・順序には使わない。Researchは三入口の対象選定、固定入力、有限調査、Stock Assessment/ReviewとInvestment Proposalを所有する。

Webのengine入口は`read_api`、Batchは`batch_api`/`read_api`である。Batchは判断や書込条件を複製せず、domainの既存serviceへ委譲する。domainが観測の意味・重複・書込条件を所有し、SQL/取得adapterが共有`storage`を使う。共有storageは純粋契約と技術helperだけを参照し、分析・AI・UI・取得serviceへ依存しない。純粋な型・算術は保存や取得を読み込まない。

Portfolio/Positionは確認済み取引と保有を所有し、Researchへ逆依存しない。確認済みbroker factを扱うCLIの合成入口は保存済み判断の参照解決を渡し、Positionのdraft組立・事実記録へ委譲する。参照解決と取引事実の記録を別の責務として保つ。Operation/taskは進行状態であり、投資評価や取引事実を代行しない。

実装は単一distributionの`baibai_engine`、表示を担う`baibai_web`、運用を担う`baibai_batch`で構成する。依存制約の厳密な範囲と公開CLIの合成例外は[import-linter設定](../pyproject.toml)が検査する。

<a id="repository-map"></a>

## 3. モデル・入力・出力の所有

以下は意味の所有と公開境界の対応である。保存場所は領域数に合わせず、field・SQL・計算式はリンク先の実契約へ委譲する。

| 所有 | 主モデル・成果物 | 入力と出力 | 保存・公開入口 |
| --- | --- | --- | --- |
| Valuation | FinancialSnapshot、倍率/機械ER/FV、ER較正文脈、Projection、条件付き収益 | `valuation/metrics`が純粋L1から財務/TTM/倍率・自己レンジを計算し、`valuation/scenario`が仮定から価値/収益を測る。`valuation/calibration`は保存済み較正文脈の契約・解釈・読取を所有 | L2機械値はStock Analysis、企業固有前提はStock Assessmentの一部。領域読取と外向け`read_api/valuation`・`read_api/er_calibration_context`。[Valuation](./reference/valuation-metrics.md#machine-valuation)・[Stock Assessment](./reference/stock-assessment.md#scenario-arithmetic) |
| Market Context | Market Snapshot、Security Market Context、公式benchmark観測、Reading、MarketContextAssessment、保存判断の履歴 | `market_context/{history,prices,margin,sector,snapshot,security}`が価格・信用需給・相対状態、`indicators`が経済観測、`reading`が指標の統計・鮮度を所有。公式total-return観測契約は`observations/benchmark`、ファイル読取は`market/jpx_total_return` | 共有市場観測はmarket/lake、経済指標は指標DB、判断はapplication。`reading/query`がL2読取、`assessment`が明示判断と履歴読取、外向け入口は`read_api/market_context`。[Market Context](./reference/market-context.md#market-context-reads) |
| External Assessment | 提示値の受領履歴、出所別比較・欠測 | `external_assessment/{acquisition,observations,store,tradingview}`が取得・純粋観測型・保存adapter、`analysis`が出所/尺度/期間付きの比較を所有 | typed L1はmarket/lake、比較は保存入力から再計算。`external_assessment/query`、外向け`read_api/external_assessment`とCLI。[入力](./reference/data-sources.md#external-assessment)・[読取](./reference/market-lake.md#external-assessment-query) |
| Screening | Stock Analysis、CandidateReason、候補文脈と候補一覧 | `screening/metrics/snapshot`が領域計算へ母集団を渡して結果を組み立て、`discovery`が同runの候補文脈を確定、`calibration`が較正実行・証拠生成・選抜評価を所有 | runs storeとcalibration snapshot。`screening`の公開入口と`batch_api`。[Screening](./reference/screening-runtime.md) |
| Research | Research selection、Stock Assessment/Review、Investment Proposal | 領域結果・出典・保有を調べ、一銘柄ずつの判断/提案を作る | applicationの判断履歴。`research/selection`・`research/workspace`・既存service。[Research](../.agents/skills/research/SKILL.md) |
| Portfolio/Position | Ledger、Reservation、Holding、Outcome | 人間確認済みbroker factを記録し、保有と資本を再構築 | application。Positionの事実記録・読取service。[Portfolio](./portfolio-management.md) |
| Operation/task | Operation Session、調査進行、運用task | 明示開始・対象・成果・完了/未完了を記録 | application。`research/operation`と`tasks`のservice。AI完了を採用・約定に変換しない |

L1/L2は定期処理へ、L3は明示開始へ分ける。一つのOperationは新規または保有の単一scopeとし、保存capacityに従って開始時に対象を固定する。0件・部分完了・未完了も記録し、弱い候補で枠を埋めず、一銘柄ずつ提案する。AIの完了、人間の採用、注文、約定は別の事実・状態である。成果物間の関係は[Decision flow](./domain-language.md#decision-flow)、開始・発行の条件は各skillが所有する。

<a id="store-authority"></a>

## 4. 保存責務と正本

storageは物理保存の技術責務であり、保存DBをL3そのものと呼ばない。application DBには判断と周辺業務の両方が入り、再生成可能なL2も用途に応じて異なる保存場所を使う。runtimeの物理SQLiteはapplication・market・経済指標・screening runs・calibrationの五系統であり、三領域へ合わせて数を変えない。Readingは保存指標とdated規則からread-timeに計算し、独立DBを新設しない。

| 保存場所 | 正本・作業コピーの関係 | 意味/書込条件のowner |
| --- | --- | --- |
| R2 `baibai-stores/baibai.sqlite` / `stores/application/baibai.sqlite` | クラウド正本と判断・ledger・task・Operation等の作業コピー | 各application domain service |
| R2 `lake/` | 不変L1 object・manifest・current pointer | 観測domainの契約、既存`publish-lake` |
| `stores/market/market.sqlite` | lake所有tableのruntime copy。`source_coverage`等のstore-local dataはR2 market store、`lake_store_origin`はコピーの由来 | source domain/SQL adapter。接続・schema・hydrateは共有storage |
| `stores/screening/runs.sqlite` | 再生成可能なScreening Run・候補一覧 | Screening |
| `stores/screening/calibration/current.sqlite` | 再生成可能なcalibration snapshot | Calibration |
| `stores/market_context/indicators.sqlite` | 経済観測・vintage・取得情報の作業コピー | `market_context/indicators`の観測service・DB adapter |

データ状態の正本はクラウド、PC/Actionsは更新元である。未公開変更を正本と区別し、取得世代を保持して競合上書きを止める。コピーの取得時点と反映状態を区別し、market storeを丸ごと消してstore-local dataを失わない。serving JSONは表示用派生物であり、application DBの本文や完了履歴の代替ではない。再取得できることは改定前情報の再現を保証しない。再生成可能なL2へ一律に永久保存を要求しない。

共有技術保存は`storage/application`・`storage/observations`・`storage/lake`に配置する。source factの意味やsource別取得は`market`等のdomainに残り、market packageが共有物理保存を所有するとは呼ばない。保存・保持は[market lake](./reference/market-lake.md)、転送・復旧は[batch運用](../batch/OPERATIONS.md)を参照する。

<a id="edinet-metric-revision"></a>

EDINETのCSV/XBRL ZIPは`edinet.source_archives`で取得bytesを保持し、既存のmarket SQLite・lake公開へ載せる。ローカルZIPは再利用可能なdownload cacheであって共有正本の代替ではない。抽出したmetric/Research factは原本から再生成するmaterializationであり、元の空欄・単位・contextや未選択項目を置き換えない。既存の抽出履歴に原本がない場合、復元したことにせず履歴を保つ。独立した監査DBや全要求履歴は追加しない。

EDINET metricのrevisionは抽出意味の変更時だけ明示更新し、配置・import・transport retryでは更新しない。対象とprovenanceは[データソース](./reference/data-sources.md#edinet-metric-revision)、[反映手順](../batch/OPERATIONS.md#edinet-metrics-revision)はbatch運用が所有する。

<a id="migration-status"></a>

## 5. 現行writerと保存履歴

現行配置は[所有表](#repository-map)に集約する。Market ContextのCLIは`market-context indicators / reading / assessment`で各ownerへ直接渡し、Webも同じ読取ownerを使う。新規writerと保存済み判断の読取は区別し、旧payloadを新規判断として再発行しない。schema・参照field・履歴読取の詳細は[Market Context reference](./reference/market-context.md#現行配置と保全するidentity)、外部評価の永続名は[保存済み照会](./reference/market-lake.md#external-assessment-query)が所有する。

Readingの現行選択先は`READING_RULES_PATH`が指す`method/macro/reading/2026-08-01T100000+0900.yaml`である。保存判断が引用する元bytes/pathを保持し、名前切替のための移設・複製・revision変更を行わない。

実装・移行と通常運用の受入は区別する。進捗・未完了の受入・確認証拠は[Issue #1414](https://github.com/koumatsumoto/baibai-loop/issues/1414)へ集約し、現行仕様は本書とreferenceで管理する。

<a id="cloud-serving-layer"></a>

## 6. 実行環境と公開入口

Ubuntu Linux/POSIXを対象とし、作業分担は[Python foundation](./reference/python-foundation.md#cloud-development-boundary)に従う。SQLite編集は作業コピー上で行い、検証済み変更をクラウドへ反映する。

```text
PCの作業コピー ──条件付き更新──> R2 stores（データ状態の正本）
                                      ↑
GitHub Actions ──定期処理・転送─────────┤
       └──read model生成──> R2 serving ──> Worker ──> browser
```

`baibai-engine`はdomain操作、`baibai-web`は表示/read model、`baibai-batch`は運用の入口である。optionの正本はpublic `--help`。[docs portal](./README.md#目的別の入口)から実操作へ進む。

<a id="failure-policy"></a>

## 7. 停止条件と変更の検証

必須入力が得られない時や、壊れたstore/release/viewを次の利用者へ渡す時は止める。optionalな欠測・古さで無関係な出力まで止めず、対象値の未評価と理由を示す。正常な未作成と既存保存物の破損を区別し、個別隔離で必須集合やledgerを勝手に欠落させない。schema・coverage・世代/CAS・履歴保護を維持する。終了状態と公開/通知の成否は[batch運用](../batch/OPERATIONS.md)が所有する。

<a id="four-roles"></a>
<a id="naming-and-history"></a>

変更では配置・保存・画面・CLI・docsを同じ責務へ揃え、無価値な互換層・重複・汎用化を削る。内部の旧名・aliasは原則残さず、caller・model・serializer・CLI・test・active docsを同じ変更で揃える。provider-native fieldと保存済み判断のidentity・引用・実取引事実は内部命名と区別し、命名整理で書き換えない。意味のあるデータと既存判断の履歴を保持し、意味不変を前後比較で検証する。規則や意味の変更による再生成と、単なる配置変更を区別する。成果を産む・誤りを止める・測る・見せる役割は識別するが、定型的な枠や新機構を増やす目的にしない。採否は[開発原則](./doctrine.md#development-investment-policy)、field/type/書込条件はmodel・DB constraint・domain service、完全gateは[Python foundation](./reference/python-foundation.md#9-ci-and-local-parity)に従う。
