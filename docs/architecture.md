# アーキテクチャ

[お買い得銘柄を見つける方針](doctrine.md)を、**日次の自動観測**と**依頼ごとの投資判断**に分けて実装する。L2は問いごとに探せるようにし、L3はその依頼で実際に調べる対象を選ぶ。

## 情報と判断の流れ

1. **取得（L1）**：providerから原本または契約したnative表現を受領する。原本の公開をL2の成功待ちにしない。
2. **機械分析（L2）**：固定入力から観測・モデルを作る。ModelGenerationは入力、対象、予定モデルと結果を固定する。一つの世代でも、価格・開示・共有材料の観測日は同じとは限らない。現行の日次が発行するScreeningSelectionは、対象の普通株を問い別に探せる別IDの索引で、機械の総合順位・買い判定ではない。
3. **企業判断（L3）**：依頼に応じて調査対象・問い・比較相手をResearch Selectionに固定し、Operationで進捗と成果を管理する。ある問いに該当しない会社も、同じ世代に記録があれば比較できる。AIが企業調査と一株の価値・価格の評価を行い、別作業者が原典・算式・反証を独立検証する。その後、現金・保有・立会を踏まえた提案を作り、評価とは別に提案本文をレビューする。機械検証は構造・参照・計算を確認し、割安や採否は決めない。市場判断は独立した入口で、L2の市場モデルではない。
4. **人間の結果と表示**：提案の発行は注文・約定ではない。報告された取引等だけが台帳を変える。Webは保存された提案・台帳・成績を表示し、価格や採否を再判定しない。

## 変更する場所

| 変えたいもの | 最初に読む実装 | 正本の境界 |
| --- | --- | --- |
| 取得範囲、受領、失敗後の再開 | [provider・受領](../engine/src/baibai_engine/data_collection/)／[取得Jobs](../jobs/src/baibai_jobs/acquisition/) | L1は受領内容と時計を持つ。規格化した値で原本を置き換えない。 |
| 原本の抽出・比較値 | [観測](../engine/src/baibai_engine/machine_analysis/observations/)／[解析モデル](../engine/src/baibai_engine/machine_analysis/models/) | 観測は出典・期間・範囲・精度を持ち、モデルが計算する。[値の読み方](reference/model-observations.md)には経済的な意味と限界を置く。 |
| 日次で何を固定し、何を発行するか | [日次workflow](../.github/workflows/cloud-daily-batch-current.yml) → [受領の固定・適用](../jobs/src/baibai_jobs/integration/sources.py) → [モデル・索引の発行](../jobs/src/baibai_jobs/integration/models.py)。[固定入力](../engine/src/baibai_engine/machine_analysis/generation/inputs.py)と[索引の構築](../engine/src/baibai_engine/machine_analysis/screening/policies/discovery.py)は別。 | 生成中に外部取得しない。表示・個社照会から再選抜しない。[探索の使い方](reference/screening-runtime.md) |
| 調査対象・判断・提案 | [企業判断CLI](../engine/src/baibai_engine/cli/investment_judgment.py) → [調査の固定と進捗](../engine/src/baibai_engine/investment_judgment/workflow/)・[評価](../engine/src/baibai_engine/investment_judgment/stock_assessment/)・[提案](../engine/src/baibai_engine/investment_judgment/trade_proposal/) | [評価の根拠とReview](reference/stock-assessment.md)、[提案時の経済比較](reference/trade-proposal.md)。価値を支持するかと、今この資金を使うかは別の判断。 |
| 約定・cash・保有・成績 | [台帳](../engine/src/baibai_engine/portfolio/ledger/) → [保有](../engine/src/baibai_engine/portfolio/holdings/)・[成績](../engine/src/baibai_engine/portfolio/performance/) | 報告された事実を保ち、提案の採否や調査完了と混ぜない。 |
| 保存・公開・表示 | [保存機構](../engine/src/baibai_engine/storage/)／[公開Jobs](../jobs/src/baibai_jobs/publication/)／[Web](../web/README.md) | storageはSQLite・snapshot・条件付き転送を担当し、採否や業務規則を決めない。 |

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

**市場ごとの観測と提案の境界**：[現行のJ-Quants日足](../engine/src/baibai_engine/machine_analysis/observations/prices/jquants_normalization.py)はJ-Quantsの銘柄・行契約に従う。六つの明示nullによる売買なし判定を、実売買代金のない別市場のOHLCVに適用しない。米国株も、提供元の書式・銘柄・暦・開示は市場別に扱い、意味が一致する計算だけ共有する。IEXの足を市場全体や公式引けの代わりにせず、米国GAAPを日本固有の利益・予想に読み替えない。両市場の査読済み判断は[本番Webの「売買提案」](../web/README.md#表示の意味)で読む。米国株の判断を配信できることは、日本株の日次選抜・円台帳への接続や注文対応を意味しない。

変更時は、規則を持つ場所・同じ意味で通る入口・次に使う出力を特定する。[検証手順](development.md#変更を検証する)で、誤銘柄・異なる単位や立会・締め後の開示／後日の訂正など必要な反例、正常経路、保存済み結果の読取を区別する。処理を減らすなら、同じ条件で何を作り直さなくなったかを確認する。全体ルールのために全層の再検証・汎用バリデータ・共通計算基盤を増やさない。

## 現在の解析モデル

[Generationの実装](../engine/src/baibai_engine/machine_analysis/generation/assembly.py)が、次の[モデル](../engine/src/baibai_engine/machine_analysis/models/)の担当単位とモデルIDを決める。個々の値の意味・不足理由は[観測リファレンス](reference/model-observations.md)へ。

- **個社**：企業財務 `company_financials`、企業成長 `company_growth`、テクニカル `technical_analysis`、外部評価 `external_assessment`。
- **個社と市場**：市場環境 `market_context`。個社の相対・価格観測と市場全体の観測は別の対象に保存する。
- **L3で調べるもの**：記事に基づく産業・社会の動向は、判断に必要な場合だけ原典を調べる。日次は記事から `industry_trends` や `social_trends` を新規生成しない。保存済みのモデルは元のGeneration/Selectionから読める。

将来の株主利益・因果・重要性はここでは決めず、L3で判断する。

<a id="store-authority"></a>
## 何を正本として再開するか

| 欲しいもの | 読む対象 |
| --- | --- |
| 企業判断、Review、提案、進捗、台帳 | [application DB](../engine/src/baibai_engine/storage/sqlite/application/)。再生成cacheではない。Operationは保存した対象・資料・当時の進捗から再開する。 |
| 受領時の外部情報 | native L1の不変body・request pack・受領記録、またはsource SQLite。本文の共有は同じ受領を意味しない。 |
| 機械分析のモデルと候補 | 通常のRunsには現行公開の[Generation](../engine/src/baibai_engine/machine_analysis/generation/query.py)と[Selection](../engine/src/baibai_engine/machine_analysis/screening/selection_query.py)だけを置く。準備済みOperationの当時の根拠はAppに固定済みで、現在の値では補わない。 |
| 元結果の再計算 | 元の固定入力・releaseのmanifestと不変object・当時の実行条件が必要。共有入力には[経済原本や確認・失敗の参照](../engine/src/baibai_engine/machine_analysis/generation/economic_inputs.py)を含む。不足時に別世代や前回L2を使わない。 |
| 日次実行用のmarket | source cache SQLiteには未移行原本・運用状態も含み、丸ごと捨てられるcacheではない。[公開と復旧](../jobs/OPERATIONS.md#machine-publication)に従う。 |

固定したcutoffは、未受領資料まで含む完全な当時の市場の再現ではない。受領・モデル生成・公開・読戻しは別の到達点であり、前段の成功を後段の成功に読み替えない。
