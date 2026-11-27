# アーキテクチャ

[お買い得銘柄を見つける方針](doctrine.md)を、日本の普通株向けに**日次の自動観測**と**依頼ごとの投資判断**に分けて実装する。L2は問いごとに探せるようにし、L3はその依頼で実際に調べる対象を選ぶ。

## 情報と判断の流れ

1. **取得（L1）**：providerから原本または契約したnative表現を受領する。取得Jobsは受領を保持し、日次は到着済みの入力を使う。経済L1の正本への反映はL2より先で、後段が失敗しても戻さない。市場の引渡しを統合済みにするsealはMachine公開の後で、受領・利用可能性・sealは別。
2. **機械分析（L2）**：固定入力から観測・モデルを作る。ModelGenerationは入力、対象、予定モデルと結果を固定する。一つの世代でも、価格・開示・共有材料の観測日は同じとは限らない。現行の日次が発行するScreeningSelectionは、対象の普通株を問い別に探せる別IDの索引で、機械の総合順位・買い判定ではない。
3. **企業判断（L3）**：依頼に応じて調査対象・問い・比較相手をResearch Selectionに固定し、Operationで進捗と成果を管理する。ある問いに該当しない会社も、同じ世代に記録があれば比較できる。AIが企業調査と一株の価値・価格の評価を行い、別作業者が原典・算式・反証を独立検証する。その後、現金・保有・立会を踏まえた提案を作り、評価とは別に提案本文をレビューする。機械検証は構造・参照・計算を確認し、割安や採否は決めない。市場判断は独立した入口で、L2の市場モデルではない。
4. **人間の結果と表示**：提案の発行は注文・約定ではない。報告された取引等だけが台帳を変える。WebはAppの台帳を再生して保有を求め、取引・保存済みの成績と提案を表示する。時価等は取得できたMarket観測で補う。現在の価格で発行済み提案の採否を判定し直さない。

日本株の調査は、現行RunsのGeneration・Selectionと依頼から問い・対象を絞り、L2だけで確かめられない重要な事実をL1や発表元の一次資料で確認する。全L1・全記事を読む手順ではない。選定・評価・提案はAppに記録する。必要な日足・立会暦・銘柄情報は対応するMarketから、現金・保有はAppから読む。通常の手順は[企業判断の入口](../.agents/skills/investment-judgment/SKILL.md)、本番データを使い隔離Appで新規提案を保存・読戻す手順は[受入](../jobs/OPERATIONS.md#production-proposal-acceptance)へ。

## 変更する場所

| 変えたいもの | 最初に読む実装 | 正本の境界 |
| --- | --- | --- |
| 取得範囲、受領、失敗後の再開 | [provider・受領](../engine/src/baibai_engine/data_collection/)／[取得Jobs](../jobs/src/baibai_jobs/acquisition/) | L1は受領内容と時計を持つ。規格化した値で原本を置き換えない。 |
| 原本の抽出・比較値 | [観測](../engine/src/baibai_engine/machine_analysis/observations/)／[解析モデル](../engine/src/baibai_engine/machine_analysis/models/) | 観測は出典・期間・範囲・精度を持ち、モデルが計算する。[値の読み方](reference/model-observations.md)には経済的な意味と限界を置く。 |
| 日次で何を固定し、何を発行するか | [日次workflow](../.github/workflows/cloud-daily-batch-current.yml) → [経済L1の統合](../jobs/src/baibai_jobs/integration/economic_sources.py)・[市場受領の固定と適用](../jobs/src/baibai_jobs/integration/sources.py) → [モデル・索引の発行](../jobs/src/baibai_jobs/integration/models.py)。[固定入力](../engine/src/baibai_engine/machine_analysis/generation/inputs.py)と[索引の構築](../engine/src/baibai_engine/machine_analysis/screening/policies/discovery.py)は別。 | 生成中に提供元へ取りに行かない。表示・個社照会から再選抜しない。[探索の使い方](reference/screening-runtime.md) |
| 調査対象・判断・提案 | [企業判断CLI](../engine/src/baibai_engine/cli/investment_judgment.py) → [調査の固定と進捗](../engine/src/baibai_engine/investment_judgment/workflow/)・[評価](../engine/src/baibai_engine/investment_judgment/stock_assessment/)・[提案](../engine/src/baibai_engine/investment_judgment/trade_proposal/) | [評価の根拠とReview](reference/stock-assessment.md)、[提案時の経済比較](reference/trade-proposal.md)。価値を支持するかと、今この資金を使うかは別の判断。 |
| 約定・cash・保有・成績 | [台帳](../engine/src/baibai_engine/portfolio/ledger/) → [保有](../engine/src/baibai_engine/portfolio/holdings/)・[成績](../engine/src/baibai_engine/portfolio/performance/) | 報告された事実を保ち、提案の採否や調査完了と混ぜない。 |
| 保存・公開・表示 | [保存機構](../engine/src/baibai_engine/storage/)／[Machineの切替](../jobs/src/baibai_jobs/publication/machine_current.py)／[Webの表示生成と配信](../web/README.md#実装と配信) | storageはSQLite・snapshot・条件付き転送を担当し、採否や業務規則を決めない。公開と表示はデータの正本を決める処理と保存済みデータを読む処理に分ける。 |

[baibai-engine](../engine/src/baibai_engine/cli/app.py)は人間・AI向けCLI、[baibai-jobs](../jobs/src/baibai_jobs/cli.py)は取得・統合・公開用。引数は各CLIのhelp、実行・検証は[開発手順](development.md)、公開や障害対応は[運用手順](../jobs/OPERATIONS.md)に従う。importの向きはデータの流れとは別で、許可境界は[pyproject.toml](../pyproject.toml)が持つ。

## 責務を変えるときの設計原則

**同じ意味の処理は同じ方針で、異なる意味の処理は分ける。** まず不要な処理をなくせるか確認する。残す処理は、その値や規則の意味を決める実装に集め、呼び出し側で作り直さない。見た目が似ているだけで、更新・失敗・依存の異なる処理を共通関数にまとめない。

<a id="external-input-contract"></a>
### 外部入力をいつ検証し、どこで計算するか

| 境界 | その場で確かめ、次へ渡すもの |
| --- | --- |
| **受領（L1）** | 取得ownerは許諾された原本、実受領時刻と失敗の記録を固定する。要求した提供元・版・範囲、必要なページ、本文の完全性が揃ったときだけ取得完了とする。同じ受領の再保存で重複を作らず、同じ本文でも別受領は区別する。途中の取得や障害を正常な空集合にしない。例えば [FRED CSV](../engine/src/baibai_engine/data_collection/providers/fred.py) の構文破損は取得側の失敗であり、観測がゼロ・欠測・消失した証拠ではない。 |
| **解釈（提供元の書式から内部の型へ）** | 原本を最初に解釈するadapterが、形式・必須項目・型・表せる範囲を検証する。曖昧な日付、真偽値の数値化、非有限値を受け入れず、出典・原単位・精度を伴う型付き観測を渡す。原本を書き換えず、同じ変換をcallerで繰り返さない。同じ観測キーで値が競合する場合は到着順だけで選ばない。 |
| **採用・計算（観測／モデル）** | 観測ownerが対象・版・時点を定め、モデルownerが用途に適した根拠と算式を選ぶ。受領成功は採用の証拠ではない。必須根拠がない値は理由付きで未算出とし、正当なゼロ、非該当、未到着、異常を区別する。独立した値まで止めない。算式を共有するのは問い・入力の意味（単位・分母・符号・基準）・適用条件・不足時の扱いが同じ場合だけ。対象・固定入力・時点まで同じ処理は一度行い、値・期間・根拠を一緒に再利用する。推計と実測、観測値と執行条件、企業価値と資金配分は別にする。 |
| **固定・保存・読取** | 保存ownerは参照・版と整合性を同じ読取トランザクション、または本文と同じ版を示す応答・manifestで確認する。例えば [L3の調査入力](../engine/src/baibai_engine/investment_judgment/workflow/selection_query.py) は同じRunsの版で候補と元の分析結果を読む。固定参照の欠落・破損は通常の欠測とは別に扱う。書込直前の競合・受付条件は別の検査として残す。CLIとWebは保存値を読み、現在の価格や規則で過去の判断を作り直さない。発行・配信・読戻しは別の成功条件。 |

- **銘柄・価格**：発行体と取引銘柄（市場・株式クラス）を分け、ティッカーは有効期間付きの別名とする。価格には通貨・単位と提供元／feed、現地の取引日と立会範囲、原本の版を結び付ける。休場・短縮・夏時間は市場の暦で判断し、受領側のJST日付では代用しない。価格の定義（提供元の集計値／公式引け）と調整（未調整／分割調整／配当込み）は別軸とし、異なるfeedや定義を欠損補完で混ぜない。
- **開示・会計**：会計基準・連結範囲と実績／予想、期間（単独／累計）と期末残高、通貨・桁・総額／一株当たりを、元の文書・版・該当箇所に結び付ける。対象日と、提出・受付・公開・実受領・訂正の各時刻は別に扱う。cutoff時点の利用可能性と、その世代が参照する実受領版を確かめて採用する。後日取得した最新版や提出・受付時刻だけを「当時利用可能」の証拠にせず、訂正前の固定結果も書き換えない。換算が必要な場合は、為替も通貨ペア・基準・時点・由来を固定する。

**市場ごとの観測と調査**：[現行のJ-Quants日足](../engine/src/baibai_engine/machine_analysis/observations/prices/jquants_normalization.py)の六つの明示nullによる「売買なし」は他市場のOHLCVに適用しない。提供元の書式・銘柄・暦・開示は市場別に扱い、意味が一致する計算だけ共有する。[米国株の実験CLI](reference/us-research-mvp.md)はAlpaca IEXとSECの受領済み資料から調査候補・保留理由を返す。IEX集計を市場全体の足や公式引けに使わず、米国GAAPを日本固有の利益・予想に読み替えない。このCLIは取得・正式提案の発行・注文を行わず、日本株の日次、App、円台帳、本番配信には接続しない。

**米国株の保存済み判断**：独立した査読を通したUSD建ての判断は、[米国株の保存処理](../engine/src/baibai_engine/investment_judgment/us_proposal.py)がAppに記録する。[本番Webの「売買提案」](../web/README.md#表示の意味)で「米国株」を選ぶと、直近20枠と選んだ保存判断を読める。見送り・判断保留が対象で、日本株の日次選抜・提案・進捗・円台帳と分ける。注文・口座操作・円換算は行わない。

変更時は、規則を持つ場所・同じ意味で通る入口・次に使う出力を特定する。[検証手順](development.md#変更を検証する)で、誤銘柄・異なる単位や立会・締め後の開示／後日の訂正など必要な反例、正常経路、保存済み結果の読取を区別する。処理を減らすなら、同じ条件で何を作り直さなくなったかを確認する。全体ルールのために全層の再検証・汎用バリデータ・共通計算基盤を増やさない。

## 現在の解析モデル

現行の日次は、[Generationの入口](../engine/src/baibai_engine/machine_analysis/generation/inputs.py)から[組立](../engine/src/baibai_engine/machine_analysis/generation/assembly.py)へ固定入力を渡し、次の五つのモデルを生成対象にする。個々の値の意味・不足理由は[観測リファレンス](reference/model-observations.md)へ。

- **個社**：企業財務 `company_financials`、企業成長 `company_growth`、テクニカル分析 `technical_analysis`、外部評価 `external_assessment`。[数値の定期取得](../jobs/OPERATIONS.md#external-assessment-acquisition)はMonexと株マップ。個社TradingViewは必要時のL3補助で、多市場のTradingView観測とは別。
- **個社と市場**：市場環境 `market_context`。個社の相対・価格観測と市場全体の観測は別の対象に保存する。経済時系列の取得は続け、記事とは分ける。

産業・社会の動向は、判断に効くときだけL3で調べる。記事の定期・手動一括取得と日次統合は終了し、`industry_trends`・`social_trends` の新規日次生成はしない。[見出し](reference/stock-assessment.md#recent-news-research)は原典を探す入口であって、本文を読んだ証拠ではない。[評価の七観点](reference/stock-assessment.md#analysis-domain-integration)は五つの機械モデルとこの二つの調査観点で判断を説明するためのもので、七つの必須モデルでも採否の多数決でもない。

<a id="store-authority"></a>
## 何を正本として再開するか

| 欲しいもの | 正本と読取の境界 |
| --- | --- |
| 市場入力・機械分析・候補 | `baibai-stores` の [`lake/pointers/market/current.json`](../jobs/OPERATIONS.md#machine-publication) が指すLake releaseと一組のMarket・Runs。通常は日次の `model-integration` が一組を切り替える。固定 `.bak` や単体の古いファイルではなく同じポインタから読む。MarketはLakeを作業用に展開するだけでなく、未移行原本・運用状態も含む。[Runs](reference/screening-runtime.md)は現行のGeneration・Selectionだけを置く。 |
| 経済原本 | [`indicators.sqlite`](../jobs/OPERATIONS.md#economic-acquisition) は経済L1の独立した正本。L2は同じ日次が読戻した経済DBから固定した[経済入力](../engine/src/baibai_engine/machine_analysis/generation/economic_inputs.py)を読む。 |
| 個別の原典・受領 | providerごとのnative L1の原本・受領記録、または所管するSQLite等。同じ本文でも別受領は別。保存済みの資料を確認する場合は指定された出典・受領を読み、現在の値で埋めない。 |
| 企業判断、Review、提案、進捗、台帳 | 独立した[`baibai.sqlite`](../jobs/OPERATIONS.md#application-db-を反映する)（App）。Operationは保存した対象・根拠・進捗から再開する。未保存の旧世代を現在の値で補わない。 |
| Webの表示 | [配信用の表示データ](../web/README.md#実装と配信)はAppと取得できたMarket観測から作る。単独の表示生成ではMarketが読めなくてもAppの内容を出せるが、未確認の時価は埋めない。`baibai-serving` のviewは派生物であり、正本ではない。 |

新規提案は今使う入力と必要な出典を確認し、過去の任意時点の再現を前提にしない。保存済み判断は保存当時の根拠を読む。Machineの公開・読戻し、市場受領の確定、Webの反映、新規提案の受入は各々別であり、一つの成功を別の成功とみなさない。
