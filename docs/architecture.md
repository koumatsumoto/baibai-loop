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
| 日次で何を固定し、何を発行するか | [日次workflow](../.github/workflows/cloud-daily-batch.yml) → [受領の固定・適用](../jobs/src/baibai_jobs/integration/sources.py) → [モデル・索引の発行](../jobs/src/baibai_jobs/integration/models.py)。[固定入力](../engine/src/baibai_engine/machine_analysis/generation/inputs.py)と[索引の構築](../engine/src/baibai_engine/machine_analysis/screening/policies/discovery.py)は別。 | 生成中に外部取得しない。表示・個社照会から再選抜しない。[探索の使い方](reference/screening-runtime.md) |
| 調査対象・判断・提案 | [企業判断CLI](../engine/src/baibai_engine/cli/investment_judgment.py) → [調査の固定と進捗](../engine/src/baibai_engine/investment_judgment/workflow/)・[評価](../engine/src/baibai_engine/investment_judgment/stock_assessment/)・[提案](../engine/src/baibai_engine/investment_judgment/trade_proposal/) | [評価の根拠とReview](reference/stock-assessment.md)、[提案時の経済比較](reference/trade-proposal.md)。価値を支持するかと、今この資金を使うかは別の判断。 |
| 約定・cash・保有・成績 | [台帳](../engine/src/baibai_engine/portfolio/ledger/) → [保有](../engine/src/baibai_engine/portfolio/holdings/)・[成績](../engine/src/baibai_engine/portfolio/performance/) | 報告された事実を保ち、提案の採否や調査完了と混ぜない。 |
| 保存・公開・表示 | [保存機構](../engine/src/baibai_engine/storage/)／[公開Jobs](../jobs/src/baibai_jobs/publication/)／[Web](../web/README.md) | storageはSQLite・snapshot・条件付き転送を担当し、採否や業務規則を決めない。 |

[baibai-engine](../engine/src/baibai_engine/cli/app.py)は人間・AI向けCLI、[baibai-jobs](../jobs/src/baibai_jobs/cli.py)は取得・統合・公開用。引数は各CLIのhelp、実行・検証は[開発手順](development.md)、公開や障害対応は[運用手順](../jobs/OPERATIONS.md)に従う。importの向きはデータの流れとは別で、許可境界は[pyproject.toml](../pyproject.toml)が持つ。

## 現在の解析モデル

[Generationの実装](../engine/src/baibai_engine/machine_analysis/generation/assembly.py)が、次の[モデル](../engine/src/baibai_engine/machine_analysis/models/)の担当単位とモデルIDを決める。個々の値の意味・不足理由は[観測リファレンス](reference/model-observations.md)へ。

- **個社**：企業財務 `company_financials`、企業成長 `company_growth`、テクニカル分析 `technical_analysis`、外部評価 `external_assessment`。
- **個社と市場**：市場環境 `market_context`。個社の相対・価格観測と市場全体の観測は別の対象に保存する。
- **共有**：産業動向 `industry_trends` は業種ごと、社会動向 `social_trends` は共通。取得した全記事に意味抽出・業種対応があるわけではない。

将来の株主利益・因果・重要性はここでは決めず、L3で判断する。

<a id="store-authority"></a>
## 何を正本として再開するか

| 欲しいもの | 読む対象 |
| --- | --- |
| 企業判断、Review、提案、進捗、台帳 | [application DB](../engine/src/baibai_engine/storage/sqlite/application/)。再生成cacheではない。Operationは保存した対象・資料・当時の進捗から再開する。 |
| 受領時の外部情報 | native L1の不変body・request pack・受領記録、またはsource SQLite。本文の共有は同じ受領を意味しない。 |
| モデルと候補の当時の結果 | [保存Generation](../engine/src/baibai_engine/machine_analysis/generation/query.py)と[保存Selection](../engine/src/baibai_engine/machine_analysis/screening/selection_query.py)をIDで読む。入力の時点や未生成を現在の値で補わない。 |
| 元結果の再計算 | 元の固定入力・releaseのmanifestと不変object・当時の実行条件が必要。共有入力には[経済原本や確認・失敗の参照](../engine/src/baibai_engine/machine_analysis/generation/economic_inputs.py)を含む。不足時に別世代や前回L2を使わない。 |
| 日次実行用のmarket | source cache SQLiteには未移行原本・運用状態も含み、丸ごと捨てられるcacheではない。[公開と復旧](../jobs/OPERATIONS.md#machine-publication)に従う。 |

固定したcutoffは、未受領資料まで含む完全な当時の市場の再現ではない。受領・モデル生成・公開・読戻しは別の到達点であり、前段の成功を後段の成功に読み替えない。
