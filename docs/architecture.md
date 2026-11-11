# Architecture

投資方針は[doctrine](doctrine.md)、判断の開始/再開は[skill](../.agents/skills/investment-judgment/SKILL.md)、取得/公開/復旧は[OPERATIONS](../jobs/OPERATIONS.md)に置く。

## 情報と判断の流れ

外部source → L1原本/受領 → L2観測/解析モデル → ModelGeneration → ScreeningSelection → Research Selection/Operation → L3企業調査/評価/独立検証 → Trade Proposal → 人間の事実報告 → Portfolio。

一次資料・共通観測・PortfolioもL3へ直接渡し、候補外も同じGenerationを使える。必須DAGやagent群ではない。

- **L1 / Data Collection**は受領原本または明示したprovider-native表現。**Observations**はその投影・正規化で、原本を代替しない。
- **L2 / Machine Analysis**は入力と規則から再現できる観測・計算・候補抽出。解析モデルは分析観点で、機械学習に限らない。**ModelGeneration**（以下Generation）は固定入力・対象・予定モデルと結果のまとまりで、観測日は同一とは限らない。**ScreeningSelection**は保存モデルへ候補政策を適用した候補と理由で、モデル生成・追加や買付採否とは別。
- **L3 / Investment Judgment**は部分的企業理解（Research）、価値・価格・成立条件（Assessment）、別作業者の根拠・仮定・算術・反証確認（Review）、現価格/cash/保有への適用（Proposal）を担う。算術は仮定を事実にせず、Reviewは安全保証や注文承認ではない。
- **Research Selection**は明示依頼の有限対象・問い・入力で、機械候補のScreeningSelectionと分ける。**Operation**は進行・成果・再開を管理する。completedと再開はskillへ。**Market Assessment**は独立したL3入口で、L2のMarket Contextとは別。

解釈はAI、算術・保存・状態管理はエンジンが担う。Signalは独立した一票でなく、Snapshotは入力/保存物の固定で完全PITではない。

## 変更する場所

業務ownerは`engine/src/baibai_engine/`。規則・型・理由・testを一緒に置き、CLI/Jobs/Webへ複写しない。許可importは[pyproject.toml](../pyproject.toml)が正本。

| 配置 | 責務 |
| --- | --- |
| `data_collection/` | provider通信/native契約、受領identity、transport、取得合成。分析分野とproviderを混ぜない。 |
| `machine_analysis/observations/` | 原本の投影/抽出と帰属・範囲・精度。 |
| `machine_analysis/models/` | 解析の問い・計算・結果。候補政策を知らない。 |
| `machine_analysis/generation/` | 固定入力・モデル実行・保存/読取。途中で外部取得しない。 |
| `machine_analysis/screening/` | 保存モデルへの候補政策と結果保存。 |
| `machine_analysis/historical_evaluation/` | 保存旧run/Selectionの投影と凍結context読取。 |
| `investment_judgment/` | L3判断。workflowは固定/進行/再開、retrospectiveは後日照合と学び。 |
| `portfolio/` | 確認済みledger、holdings、performance。採否/調査完了は持たない。 |
| `storage/` | SQLite・snapshot・object転送/条件付き書込。業務の意味は決めない。 |
| `primitives/`・`cli/` | 小さな共通値/変換、引数/表示/終了コード。汎用業務箱にしない。 |

Collection→L2、観測→モデル、生成→政策、Portfolio→L3の依存を作らない。L2はL1と明示設定から再生成し、前回L2を必須にしない。取得進捗は原本と分け、消失時の再取得条件を持つ。逆依存の解消は関係データの廃棄許可ではない。

保存契約は原本/観測owner、検証機構はStorage、公開対象/source別受入はJobsが持つ。全体hydrate/dehydrateには完全なcatalog/policy、個別固定読取には必要なowner契約を渡す。

`jobs/src/baibai_jobs/`は取得・統合・公開・保守・監視、`tools/`は開発/診断。[Web](../web/README.md)は資産・成果・保存提案の読取を担い、判断作成・現在preview・台帳更新やcore成功条件を持たない。

## 現在の解析モデル

| 日本語 / 英語 | 問い |
| --- | --- |
| 企業財務 / Company Financials | 財務状態・収益力・利益/資本と株価の比較。 |
| 企業成長 / Company Growth | 実績の状態と変化。 |
| 市場環境 / Market Context | 経済・市場・業種・個社の相対状態。 |
| 外部評価 / External Assessment | 外部予想/rating/評価の比較。 |
| テクニカル分析 / Technical Analysis | 価格位置・勢い・変動。 |
| 産業動向 / Industry Trends | 母集団・期・利益basisを保つ業界比較。 |
| 社会動向 / Social Trends | 帰属/範囲を持つ制度・政策・災害等の主張・予定・変更。 |

通常生成の産業動向はTSRの医薬品・医療用品卸売業/情報サービス業の業績調査、社会動向の意味抽出は日銀の金融市場調節方針決定。原本取得・保持範囲とは別で、未抽出・未対応を事象なしとしない。個社・業種共有・市場共有subjectと、不変抽出artifact・原受領を分ける。

帰属・範囲・否定・条件・精度・原文座標と記事の有限選択は[Observations](reference/model-observations.md)、比較はモデル、入力と銘柄の関連づけはGenerationが持つ。業界対応を推測せずversion付き母集団・除外・出典を残し、lineage releaseと本文検索のlookup releaseを分ける。将来価値・将来成長の見込み・重要性/因果はL3が判断する。候補政策は[Screening](reference/screening-runtime.md)へ。

<a id="store-authority"></a>
## 正本と再開

| 保存単位 | 正本/寿命 |
| --- | --- |
| application DB | Research・評価/Review・提案・Research Selection/Operation・台帳。再生成cacheではない。 |
| native L1 | 可変head＋不変body/request pack、またはsource SQLiteの原本/受領。body共有と受領記録は別。 |
| 共有固定入力 | 採用原本/確認/失敗のmetadataとL1本文参照。保存入力の再計算に必要な間保持する。 |
| 固定lake | L2と未移行L1のimmutable object/manifest。同じreleaseで読む。旧releaseの固定読取も保持する。 |
| source cache SQLite | market実行copy。未移行原本/運用状態を含み、全体は廃棄可能cacheではない。 |
| runs / 旧calibration | ModelGeneration/ScreeningSelectionを保存する。再計算には保持入力と当時の実行条件が必要で、旧run/較正は[読取専用](reference/estimate-calibration.md)で保持する。 |

原本受領・原本公開・L2公開は別の成功で、独立L1をL2成功待ちにしない。生成/公開は開始入力を固定し、未生成と結果内欠測を分ける。再開で現在のモデル一覧・既定値・latest headを補わない。L3は必要断面をapplicationへ保存し、元runや可変storeに依存せず再開する。整理/GCは原本・判断・取引履歴・R2全体の削除許可ではない。

原本/全履歴の日次複写を避け、必要投影と不変objectを再利用する。実装済みcold再生成・旧release依存・source別の取得/保持/復旧は[観測](reference/model-observations.md#source-owners)とOPERATIONSへ。保持/容量/時間の実データ受入は[#1820](https://github.com/koumatsumoto/baibai-loop/issues/1820)に残る。

[経済固定入力](../engine/src/baibai_engine/machine_analysis/generation/economic_inputs.py)は当時採用した原本と確認/失敗を固定する。後日訂正や採用へ差替えず、冷再計算には保持原本と当時のengine/config/runtimeを要する。原本欠落は当時欠測でなく失敗。通常生成では、同じ実行でR2から検証済みの不変metadataだけを上限付きで次の再構築へ渡し、直後の重複通信を避ける。別実行のcacheやL2を原本の代用にせず、原本rowとrecipeの検査、cold読取のremote検証を保つ。旧hash-only世代の失われたDBは復元できず、全generation exact再生・過去市場PIT・一律I/O上限も保証しない。

配置・政策変更でも保存identityと再開入力を保ち、配置だけでkey/ID/hash/意味revision・原bytes・受領時刻・source identityを変えない。Company Financialsの旧Selection canonicalは元公開hashの根拠として[保持](../jobs/OPERATIONS.md#financials-identity-cutover)し、通常readerへ旧aliasを戻さない。

Stocksは保有に依存しない。Jobは実行/成否/再開の単位、Workflowはその組立て、Batchは非対話方式である。Snapshot PublicationもL1/L2を含み得る公開で、これらを解析モデルや情報層と一対一にしない。
