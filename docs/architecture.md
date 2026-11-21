# アーキテクチャ

このシステムには、**日次に観測を作る処理**と、**人間の依頼から始まる投資判断**の二つの入口がある。観測は「何が違えばこの価格は安いか」を調べる材料で、買いの票ではない。

## 情報と判断の流れ

1. **取得（L1）**：providerから原本または契約したnative表現を受領する。原本の公開をL2の成功待ちにしない。
2. **機械分析（L2）**：固定入力から観測・モデルを作る。ModelGenerationは入力、対象、予定モデルと結果を固定する。一つの世代でも、価格・開示・共有材料の観測日は同じとは限らない。ScreeningSelectionはその世代から発行する探索索引で、別のIDを持つ。
3. **企業判断（L3）**：依頼に応じて調査対象・問い・比較相手をResearch Selectionに固定し、Operationで進捗と成果を管理する。候補外の会社も同じ世代から読める。企業調査 → 一株の価値と価格の評価 → 別作業者の独立検証 → cash・保有・立会を考慮した提案へ進む。市場判断は独立した入口で、L2の市場モデルではない。
4. **人間の結果と表示**：提案の発行は注文・約定ではない。報告された取引等だけが台帳を変える。Webは保存された提案・台帳・成績を表示し、価格や採否を再判定しない。

## 変更する場所

| 変えたいもの | 最初に読む実装 | 正本の境界 |
| --- | --- | --- |
| 取得範囲、受領、失敗後の再開 | [provider・受領](../engine/src/baibai_engine/data_collection/)／[取得Jobs](../jobs/src/baibai_jobs/acquisition/) | L1は受領内容と時計を持つ。規格化した値で原本を置き換えない。 |
| 原本の抽出・比較値 | [観測](../engine/src/baibai_engine/machine_analysis/observations/)／[解析モデル](../engine/src/baibai_engine/machine_analysis/models/) | 観測は出典・期間・範囲・精度を持ち、モデルが計算する。[値の読み方](reference/model-observations.md)には経済的な意味と限界を置く。 |
| 日次で何を固定し、何を発行するか | [日次workflow](../.github/workflows/cloud-daily-batch.yml) → [統合](../jobs/src/baibai_jobs/integration/models.py) → [Generationの入力](../engine/src/baibai_engine/machine_analysis/generation/inputs.py)・[探索索引の生成](../engine/src/baibai_engine/machine_analysis/screening/policies/discovery.py) | 生成中に外部取得しない。表示・個社照会から再選抜しない。[探索の使い方](reference/screening-runtime.md) |
| 調査対象・判断・提案 | [企業判断CLI](../engine/src/baibai_engine/cli/investment_judgment.py) → [調査の固定と進捗](../engine/src/baibai_engine/investment_judgment/workflow/)・[評価](../engine/src/baibai_engine/investment_judgment/stock_assessment/)・[提案](../engine/src/baibai_engine/investment_judgment/trade_proposal/) | [評価の根拠とReview](reference/stock-assessment.md)、[提案時の経済比較](reference/trade-proposal.md)。価値を支持するかと、今この資金を使うかは別の判断。 |
| 約定・cash・保有・成績 | [台帳](../engine/src/baibai_engine/portfolio/ledger/) → [保有](../engine/src/baibai_engine/portfolio/holdings/)・[成績](../engine/src/baibai_engine/portfolio/performance/) | 報告された事実を保ち、提案の採否や調査完了と混ぜない。 |
| 保存・公開・表示 | [保存機構](../engine/src/baibai_engine/storage/)／[公開Jobs](../jobs/src/baibai_jobs/publication/)／[Web](../web/README.md) | storageはSQLite・snapshot・条件付き転送を担当し、採否や業務規則を決めない。 |

[baibai-engine](../engine/src/baibai_engine/cli/app.py)は人間・AI向けCLI、[baibai-jobs](../jobs/src/baibai_jobs/cli.py)は取得・統合・公開用。引数は各CLIのhelp、実行・検証は[開発手順](development.md)、公開や障害対応は[運用手順](../jobs/OPERATIONS.md)に従う。importの向きはデータの流れとは別で、許可境界は[pyproject.toml](../pyproject.toml)が持つ。

## 現在の解析モデル

[企業財務・企業成長・市場環境・外部評価・テクニカル分析・産業動向・社会動向](../engine/src/baibai_engine/machine_analysis/models/)が、個社・業種共有・市場共有の対象を別々に扱う。企業財務は株価と利益・資本を比べ、企業成長は実績の状態と変化を測る。産業や社会の記事は対象と期間を保ち、取得した全記事に意味抽出・業種対応があるわけではない。将来の株主利益・因果・重要性はL3で判断する。

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
