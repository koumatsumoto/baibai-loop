# Architecture

Baibai Loopは日本個別株の価値・価格・リスク・機会費用を調べ、根拠付き提案を作る。最終採否・注文・約定確認は人間が担う。方針は[doctrine](doctrine.md)、判断の開始/再開は[skill](../.agents/skills/investment-judgment/SKILL.md)、取得/公開/復旧は[OPERATIONS](../jobs/OPERATIONS.md)へ進む。

## 情報と判断の流れ

外部source → L1原本/受領 → L2観測/解析モデル → 固定Generation → Screening/Selection → L3企業調査/評価/独立検証 → Trade Proposal → 人間の事実報告 → Portfolio。

一次資料・共通観測・PortfolioもL3へ直接渡す。候補外も同じGenerationを使え、この順を必須DAGやagent群へ変えない。

- **L1 / Data Collection**は受領原本または明示したprovider-native表現。**共通観測 / Observations**はその投影・正規化で、原本の代替ではない。
- **L2 / Machine Analysis**は入力と規則から再現できる観測・計算・候補抽出。解析モデルは分析観点（機械学習に限らない）、Generationは固定入力の対象・予定モデルと結果のまとまりで、各観測日は同一とは限らない。候補政策は保存モデルから調査候補と理由を選び、モデル生成や買付採否とは別。モデル追加だけでは候補政策を変えない。
- **L3 / Investment Judgment**は一次資料による部分的企業理解（Research）、価値・支払価格・成立条件（Assessment）、別作業者による根拠・仮定・算術・反証の確認（Review）、検証済み評価の現価格/cash/保有への適用（Proposal）を扱う。決定的な算術も仮定を事実にせず、Reviewは無リスク認定や注文承認ではない。
- **Selection**は明示依頼の有限対象・問い・必要入力を固定し、**Operation**は進行・成果・再開を管理する。completedの意味と再開はskillに従う。市場環境判断（Market Assessment）は独立したL3入口で、L2のMarket Contextとは別。

解釈はAI、算術・保存・状態管理はエンジンが担う。Signalは判断材料で独立した一票ではなく、Snapshotは入力/保存物の固定で完全PITではない。情報層は実行主体・工程数・snapshotと一対一ではない。

## 変更する場所

業務ownerは`engine/src/baibai_engine/`。規則・型・理由・testを一緒に置き、CLI/Jobs/Webへ複写しない。許可importは[pyproject.toml](../pyproject.toml)が正本。

| 配置 | 責務 |
| --- | --- |
| `data_collection/` | provider通信/native契約、受領identity、transport、取得合成。分析分野とproviderを混ぜない。 |
| `machine_analysis/observations/` | 原本の投影/抽出と帰属・範囲・精度。 |
| `machine_analysis/models/` | 下表の問い・計算・結果。候補政策を知らない。 |
| `machine_analysis/generation/` | 固定入力・モデル実行・保存/読取。途中で外部取得しない。 |
| `machine_analysis/screening/` | 保存モデルへの候補政策と結果保存。 |
| `machine_analysis/historical_evaluation/` | 保存旧run/Selectionの投影と凍結context読取。旧ER/FVの再推計・再選抜・較正再生成はしない。 |
| `investment_judgment/` | L3判断。workflowは固定/進行/再開、retrospectiveは後日照合と学び。 |
| `portfolio/` | 確認済みledger、holdings、performance。採否/調査完了は持たない。 |
| `storage/` | SQLite・snapshot・object転送/条件付き書込。業務の意味は決めない。 |
| `primitives/`・`cli/` | 小さな共通値/変換、引数/表示/終了コード。汎用業務箱にしない。 |

Collection→L2、観測→モデル、生成→政策、Portfolio→L3の依存を作らない。L2はL1と明示設定から再生成し、前回L2を必須正本にしない。取得進捗は原本と分け、消失時の再取得条件を持つ。残る逆依存は移行対象であり、関係データの廃棄許可ではない。

保存契約は原本/観測owner、検証機構はStorage、公開対象/source別受入はJobsが持つ。全体hydrate/dehydrateには完全なcatalog/policy、個別固定読取には必要なowner契約を渡す。

`jobs/src/baibai_jobs/`は取得・統合・公開・保守・監視、`tools/`は開発/診断で、通常運用を隠さない。[Web](../web/README.md)は資産・成果・保存提案/exact pairの読取だけを担い、判断作成・現在preview・台帳更新やcore成功条件を持たない。

## 現在の解析モデル

| 日本語 / 英語 | 問いと利用境界 |
| --- | --- |
| 企業財務 / Company Financials | 財務状態・収益力・利益/資本と株価の比較。将来価値ではない。 |
| 企業成長 / Company Growth | 実績の状態と変化。将来成長の認定ではない。 |
| 市場環境 / Market Context | 経済・市場・業種・個社の相対状態。市場判断ではない。 |
| 外部評価 / External Assessment | 外部予想/rating/評価の比較。自分の判断と分ける。 |
| テクニカル分析 / Technical Analysis | 価格位置・勢い・変動。急落だけを固定入口にしない。 |
| 産業動向 / Industry Trends | 母集団・期・利益basisを保つ業界比較。通常生成はTSRの医薬品・医療用品卸売業/情報サービス業の業績調査。利益/供給能力への推測はL3。 |
| 社会動向 / Social Trends | 制度・政策・災害等の帰属/範囲を持つ主張・予定・変更。通常生成の意味抽出は日銀の金融市場調節方針決定。重要性/因果はL3。 |

通常生成の範囲を原本取得・保持範囲へ適用せず、未抽出・未対応を事象なしとしない。個社・業種共有・市場共有subjectを分け、不変抽出artifactも原受領から独立させる。

帰属・範囲・否定・条件・精度・原文座標と記事の有限選択はObservations、比較はモデル、入力と銘柄の関連づけはGenerationが持つ。業界対応を推測せずversion付き業界・母集団・除外・出典を残し、lineage releaseと本文を探すlookup releaseを分ける。モデル追加・生成接続・政策変更は別で、候補の意味は[Screening](reference/screening-runtime.md)に従う。

<a id="store-authority"></a>
## 正本と再開

| 保存単位 | 正本/寿命 |
| --- | --- |
| application DB | Research・評価/Review・提案・Selection/Operation・台帳。再生成cacheではない。 |
| native L1 | 可変head＋不変body/request pack、またはsource SQLiteの原本/受領。body共有と受領記録は別。 |
| 共有固定入力 | 採用原本/確認/失敗のmetadataとL1本文参照。保存入力の再計算に必要な間保持する。 |
| 固定lake | L2と未移行L1のimmutable object/manifest。同じreleaseで読む。 |
| source cache SQLite | market実行copy。未移行原本/運用状態を含み、全体は廃棄可能cacheではない。 |
| runs / 旧calibration | モデル世代/政策結果は再生成可能。旧run/較正結果は保持し、旧方式は再生成・再公開しない。 |

原本受領・原本公開・L2公開は別の成功で、独立L1をL2成功待ちにしない。生成/公開は開始入力を固定し、未生成と結果内欠測を分ける。再開で現在のモデル一覧/既定field/latest headを補わない。L3は必要断面をapplicationへ保存し、元runや可変storeからの再計算に依存しない。整理/GCは原本・判断・取引履歴・R2全体の削除許可ではない。

設計上は原本/全履歴の日次複写を避け、必要投影と不変objectを再利用する。実装済みcold再生成と旧release依存、source別の取得/保持/復旧は[観測](reference/model-observations.md#source-owners)とOPERATIONSで確認する。実データの保持/容量/時間受入は[#1820](https://github.com/koumatsumoto/baibai-loop/issues/1820)に残る。

[経済固定入力](../engine/src/baibai_engine/machine_analysis/generation/economic_inputs.py)は当時採用した原本と確認/失敗を固定する。後日訂正や採用へ差替えず、冷再計算には保持原本と当時のengine/config/runtimeを要する。原本欠落は当時欠測でなく失敗。旧hash-only世代の失われたDBは復元できず、全generation exact再生・過去市場PIT・一律I/O上限も保証しない。

配置変更はkey/ID/hash/意味revisionを変えず、原bytes・受領時刻・source identityを保つ。政策変更でも保存identityと再開入力は不変。移行済みCompany Financialsの旧Selection canonicalは元公開hashの根拠として保持し、[旧出典](../jobs/OPERATIONS.md#financials-identity-cutover)を読み、通常readerへ旧aliasを戻さない。

Stocksは保有に依存しない。Jobは実行/成否/再開の単位、Workflowはその組立て、Batchは非対話方式である。Snapshot PublicationもL1/L2を含み得る公開で、これらを解析モデルや情報層と一対一にしない。
