# Architecture

Baibai Loopは、外部から受領した事実を保存し、機械分析から調査の入口を作り、企業の経済的な価値と支払価格を比較する。AIは根拠付きの提案を作り、人間が採否・注文・約定を確認する。

## 情報の流れ

```mermaid
flowchart LR
    S[外部source] --> C[L1 Data Collection]
    C --> M[L2 Machine Analysis: Model Generation]
    M --> F[L2 Screening]
    M --> J[L3 Investment Judgment]
    F -->|候補と抽出理由| J
    I[一次資料・確認済み保有] --> J
    J --> H[システム外: Human Decision → Order Execution]
    H -->|確認済み取引| P[Position / 台帳] --> J
```

L1 **Data Collection**は原本またはsource契約で明示したprovider-native表現と受領事実を保持する。L2 **Machine Analysis**は入力を選択・正規化・計算する**Model Generation**と、規則に従う候補抽出**Screening**である。同じ入力・規則・計算実装・基準時点なら同じ結果を返す。L3 **Investment Judgment**は仮定を伴う解釈・評価・判断である。型付き行は原本の代わりにならず、仮定を決定的な式で計算してもL2の観測にはならない。

L1〜L3は情報の性質、ジョブ・エンジン・AI・人間は実行主体の区分である。情報取得ジョブはsource別に独立して実行・再実行する。共通統合ジョブは全取得の完了を待たず、開始時に利用可能な受領と適格な保存入力からモデル生成と候補抽出を起動する。必要時も同じエンジンを使い、遅延と欠測を区別する。五モデルごとの新DBや事前計算tableは要求しない。

## 責務と配置

engineのdomainは[`engine/src/baibai_engine/`](../engine/src/baibai_engine/)に置く。意味を持つ処理はそのdomainへ、業務を知らない技術は`storage`・`foundation`へ置く。

| 配置 | 所有する責務 |
| --- | --- |
| `data_collection/` | Data Collection：sourceとの通信、原本・native record・受領の契約、取得の合成 |
| `machine_analysis/company_valuation/` | Company Valuation：[財務指標と機械ER/FV](reference/company-valuation.md)、[較正文脈](reference/estimate-calibration.md) |
| `machine_analysis/company_growth/` | Company Growth：[過去実績状態・変化](reference/company-growth.md)、会計四半期を整理軸とする振り返り |
| `machine_analysis/market_context/` | Market Context：[経済Reading、市場・業種・個別の相対状態](reference/market-context.md) |
| `machine_analysis/external_assessment/` | External Assessment：[外部提示値の出所・対象期・比較条件と欠測](reference/data-sources.md#external-assessment) |
| `machine_analysis/technical_analysis/` | Technical Analysis：[保存価格の位置・勢い・変動幅](reference/technical-analysis.md) |
| `machine_analysis/screening/` | Screening：現行のCompany Valuation・Market Contextから候補と理由を組み立て、歴史較正を行う |
| `investment_judgment/` | Company Research・Stock Assessment・Independent Review・Trade Proposal、市場判断、Selection/Operationの進行、仮定付き評価算術 |
| `market/` | 共通観測の読取・正規化・価格/財務算術と、source取得・handoff・投影の既存合成 |
| `position/`・`tasks/` | 確認済み取引・保有・資本と運用上の進行状態 |
| `storage/`・`foundation/` | 接続、schema、transaction、lakeと汎用基盤 |

五モデルはScreening専用の中間データではない。Investment Judgmentは公開読取入口から直接モデルと一次資料を統合し、候補外の企業も調査できる。現行Screeningの入力・順位・候補規則は名称整理では変えず、企業価値や買付採否を確定しない。現行のERや四つの候補抽出経路は現在の実装方式であり、正式用語・責務の確定は戦略の妥当性や将来の維持を確定するものではない。Company Valuationも真の企業価値や最終買付判断を確定するものではない。

`market/`はsource独立取得、原受領のhandoff、最新正本への投影が同じmodule群で連携している。これを丸ごとL2へ置くと新規取得まで機械分析に見え、分割にはtransaction・再開・store依存の再設計が要るため現配置を保つ。共通価格は`market/price_reader.py`が原価格・action・coverage、`adjusted_prices.py`が分析用価格、`price_basis.py`が純粋な換算を所有する。価格調整と株数単位の変更を同じ契約へ潰さない。

外部評価の取得は`data_collection/external_assessment/`、TradingView通信合成は`data_collection/tradingview/`に置く。取得と解析・保存を合成する既存CLI・collector・handoffはL1専用処理ではなく、原本保持とL2投影の境界を保った合成である。通信のない正規化・観測・読取はMachine Analysisが所有する。合成を分解して新しい保存・再開手順を導入することは今回行わない。

仮定付きProjection・return/Pmax・CB算術は`investment_judgment/`が所有する。Positionも使う有限数の変換は`foundation/numbers.py`に置き、PositionからL3への逆依存を作らない。モデルは判断・候補選択・UIへ逆依存せず、Positionは人間の確認済み事実を保存する。

`web/`はread-only表示、`jobs/`は情報取得・共通統合・表示反映等の実行の合成、`tools/`は開発・保守用の入口である。Webは`read_api`、Jobsは`job_api`・`read_api`を通し、domainの算術・保存条件を複製しない。依存範囲の厳密な定義は[pyproject.toml](../pyproject.toml)にある。

ジョブは一つの目的を持つ実行・成功失敗・再実行の単位、ワークフローはその順序・並行実行・依存関係を定めるもの。GitHub Actionsのjobと業務ジョブは常に一対一ではない。バッチ処理は非対話でまとまったデータを処理する方式を指す。workflow filenameや保存済み実行IDは名前整理だけでは改名しない。

意味・採否は本書・doctrine・各reference、開始・操作・完了/再開はskills・OPERATIONSが所有する。生成scaffoldは編集欄と短い注意・参照先を持ち、一回限りの計画・実験はIssueと固定commitへ残す。

## 判断と進行

Company Research（企業調査）は一次資料から事業・財務・競争力・経営を調べ、Stock Assessment（銘柄評価）は五モデルと調査結果を統合し、価値・価格・リスク・成立条件を評価する。Independent Review（独立検証）は作者と別の担当AIが根拠・仮定・算術・反証を検証し、Trade Proposal（売買提案）は検証済み評価を価格・cash・保有へ適用する。全事実の完全証明や無リスク認定ではない。

解釈・判断はAI、算術・保存・状態管理はエンジンが担当する。人間の明示開始から有限対象を扱い、新しい自律agent基盤や自動調査起動は設けない。Selectionは対象・問い・入力を開始時に固定する。不足・反証があれば調査・評価へ戻り、工程名を理由に固定DAGや必須承認を増やさない。個別企業から独立した市場判断は別入口を持ち、L2のMarket Contextと区別する。

最終裁定 **Human Decision**と注文実行 **Order Execution**はシステム外の人間の行為で、L4は設けない。Operation完了、提案公開、人間の採用、注文、約定は別の状態である。確認済み事実と進行状態を記録するPositionは維持する。

期間・観測日・公表日・受領cutoffを区別する。固定snapshotは再開時の入力を保つが、全入力のintraday point-in-time再現を保証しない。[入力の意味](reference/data-sources.md)と[判断の意味](reference/stock-assessment.md)が個別の制約を所有する。

| 作業の入口 | 操作先 |
| --- | --- |
| 新しい有限Investment Judgment | [Investment Judgmentの開始](../.agents/skills/investment-judgment/SKILL.md#開始と固定対象) |
| 共通市場判断だけを作る | [市場環境判断（Market Context Assessment）](../.agents/skills/market-context-assessment/SKILL.md) |
| 人間が確認した取引事実を記す | [Ledger Record](../.agents/skills/ledger-record/SKILL.md) |
| 既存Operationを続ける | [保存入力からの再開](../.agents/skills/investment-judgment/SKILL.md#完了と再開) |

3skillsは作業の開始条件で分けており、五モデルやL3四工程の数に合わせて増やさない。

<a id="store-authority"></a>
## 保存責務

データ状態の正本はクラウドにあり、認可された作業環境とActionsが更新する。未公開の作業コピーと正本を区別し、世代を照合して反映する。

| 保存場所 | 役割 |
| --- | --- |
| application DB | 判断・Review・提案・Selection・Operation・task・台帳の正本と作業コピー |
| market lake | L1原本とL2投影のimmutable object・manifest。readerは一つのreleaseを固定する |
| market SQLite | lake対象tableの実行用コピーと、取得coverage等のstore-local情報 |
| economic source SQLite | 経済原本・受領・取得記録。Readingは保存原本とmethodから読取時に計算する |
| screening runs / calibration | 再生成可能な探索結果と較正snapshot |
| serving JSON | 表示用の派生物。保存判断・台帳の第二正本にしない |

source取得は担当範囲の再生成可能なread-only inputと原受領handoffだけを扱う。共通統合は開始時に存在するimmutable受領を固定し、取得全体の完了を待たず、最新正本へ各domainのprojection/rebuildを適用して一回のfull lake公開・L2・表示を行う。source-local SQLiteは全体releaseへboundな実行コピーではなく、full originを持たせない。経済の一時受領は実取得の同値も保持し、正本への変更時追記・同値no-opは最新正本との比較で決める。

クラウド正本のapplication objectは`baibai-stores/baibai.sqlite`。ローカルpathは[repository_layout.py](../engine/src/baibai_engine/foundation/repository_layout.py)が所有する。market全体を消してstore-local情報を失わず、空のhydrateから取得成功記録を創作しない。

現行のdated規則だけを[`machine_analysis/screening/rules/`](../engine/src/baibai_engine/machine_analysis/screening/rules/)と[`machine_analysis/market_context/reading/rules/`](../engine/src/baibai_engine/machine_analysis/market_context/reading/rules/)へ配置し、既定revisionは各loaderが読む。旧版は[Git履歴](https://github.com/koumatsumoto/baibai-loop/tree/27daa52c4b63a8a2fc380e7f4f35b2e211be8e79/method)で保持し、保存結果の閲覧に再配置しない。過去計算の再現には当時の規則だけでなくコードと入力も要る。規則の意味を変える場合は新revisionとし、配置変更だけで内容・識別子・保存判断を変えない。較正の配布artifactは[`machine_analysis/company_valuation/calibration/published/`](../engine/src/baibai_engine/machine_analysis/company_valuation/calibration/published/)、テスト入力は[`tests/fixtures/`](../tests/fixtures/)、表示設定は`web/config/`に置く。

開発計測・移行の記録はIssue/PRと[固定Git履歴](https://github.com/koumatsumoto/baibai-loop/tree/27daa52c4b63a8a2fc380e7f4f35b2e211be8e79/reports)へ委ねる。正式なInvestment Judgment成果は上記application DBへ保存し、過去の開発レポートを現在の判断として再発行しない。

配置・Python名・CLI・表示名の変更では、保存済みJSONのkind/field、DB table、R2 key、ID・fingerprint・本文・履歴、規則hash/dated stem、model revisionを変えない。保存契約の`investment_proposal`・`research_selection`等はそのまま読み書きし、現在の工程名と区別する。原本bytes・受領日時・source identity・release/hash、候補集合と理由、null/0と既知不足の意味も保つ。

運用上の取得・発行・復旧は[OPERATIONS](../jobs/OPERATIONS.md)、設計判断は[doctrine](doctrine.md)、開発環境は[development](development.md)に従う。
