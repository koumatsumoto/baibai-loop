# Architecture

Baibai Loopは日本個別株の機会を調べ、価値・価格・リスク・機会費用から根拠付き提案を作る。最終採否・注文・約定確認は人間が担う。採否は[doctrine](doctrine.md)、操作は[投資判断skill](../.agents/skills/investment-judgment/SKILL.md)と[OPERATIONS](../jobs/OPERATIONS.md)へ進む。

## 情報と判断の流れ

外部source → L1原本/受領 → L2共通観測/解析モデル → 固定Generation → Screening/Selection → L3企業調査/評価/独立検証 → Trade Proposal → 人間の事実報告 → Portfolio。

一次資料・共通観測・PortfolioもL3へ直接渡す。候補外も同じGenerationを使え、工程名から必須DAGやagent群を増やさない。

情報収集（Data Collection/L1）は受領した原本または明示したprovider-native表現。機械分析（Machine Analysis/L2）は入力と規則から再現できる観測・計算・候補抽出。投資判断（Investment Judgment/L3）は仮定と解釈を伴う判断で、決定的な算術でも仮定を事実へ変えない。この情報層を実行主体や工程数、snapshotと混同しない。

| 用語 | 意味 |
| --- | --- |
| 共通観測 / Observations | 原本の投影・正規化。L1の代替ではない。 |
| 解析モデル / Analysis Model | 一つの分析観点の計算と結果。機械学習に限らず、シグナル数の多数決でもない。 |
| モデル生成 / Model Generation | 固定入力からモデルを計算・保存する処理。候補選定とは別。 |
| 生成世代 / Generation | 同じ固定入力の対象・予定モデルと結果のまとまり。各観測日は同一とは限らない。 |
| シグナル / Signal | モデル等から得る判断材料。モデルそのものや独立した一票ではない。 |
| 候補政策 / Screening Policy | 保存モデルから候補を選ぶ規則。モデル追加だけでは変わらない。 |
| 候補抽出 / Screening | 保存モデルへ候補政策を適用し、調査候補と理由を選ぶ。モデル追加や買付採否とは別。 |
| 調査対象の固定 / Selection | 明示依頼の有限対象・問い・必要入力を固定する。 |
| 判断作業の進行 / Operation | 固定対象の進行・成果・再開。completedは作業終了で、提案完成・採用・注文・約定とは限らない。 |
| 固定スナップショット / Snapshot | 入力または保存物の固定。完全な過去PITを保証しない。 |

企業調査（Company Research）は一次資料による部分的な企業理解、銘柄評価（Stock Assessment）は価値・支払価格・成立条件の評価、独立検証（Independent Review）は作者と別の根拠・仮定・算術・反証確認。売買提案（Trade Proposal）は検証済み評価を価格/cash/保有へ適用する。独立検証は無リスク認定や人間の注文承認ではない。市場環境判断（Market Assessment）は個社と独立した入口を持ち、L2のMarket Contextと分ける。解釈はAI、算術・保存・状態管理はエンジンが担う。

## 変更する場所

業務ownerは`engine/src/baibai_engine/`。一緒に変わる規則・型・理由・testを近づけ、CLI/Jobs/Webへ規則を複写しない。具体的な許可importは[pyproject.toml](../pyproject.toml)が正本。

| 配置 | 所有するもの |
| --- | --- |
| `data_collection/` | provider通信/native契約、受領identity、transport、取得合成。分析分野とproviderを混ぜない。 |
| `machine_analysis/observations/` | 共通観測と原文の投影/抽出。原典の帰属・範囲・精度を保つ。 |
| `machine_analysis/models/` | 下表の分析上の問い・計算・結果。候補政策を知らない。 |
| `machine_analysis/generation/` | 固定入力・モデル実行・保存/読取。途中で外部取得しない。 |
| `machine_analysis/screening/` | 保存モデルへの候補政策と結果保存。 |
| `machine_analysis/historical_evaluation/` | 保存旧run・旧Selectionの投影と凍結context読取。旧ER/FVの再推計・再選抜・較正再生成はしない。 |
| `investment_judgment/` | 調査・評価・Review・提案・市場判断。workflowは固定/進行/再開、retrospectiveは後日照合と学び。 |
| `portfolio/` | 確認済み取引のledger、保有holdings、資産全体のperformance。採否/調査完了は持たない。 |
| `storage/` | SQLite・固定snapshot・object転送/条件付き書込。業務上の意味は決めない。 |
| `primitives/`・`cli/` | 小さな共通値/変換、引数/表示/終了コード。汎用業務箱にしない。 |

CollectionはL2、観測はモデル、生成は政策、PortfolioはL3へ依存しない。L2はL1と明示設定から再生成し、前回L2を必須正本にしない。原本と取得進捗を分け、進捗消失時の再取得条件を持つ。未解消の逆依存は移行対象であり、関係データを廃棄可能とは扱わない。

保存契約は原本/観測owner、Storageは保存・検証機構、Jobsは公開対象とsource別受入を持つ。全体hydrate/dehydrateには完全なcatalog/policy、個別固定読取には必要なowner契約を渡す。

`jobs/src/baibai_jobs/`は取得・統合・公開・保守・監視を合成する。`tools/`は開発/診断用で、通常運用を隠さない。[Web](../web/README.md)は資産・成果・保存提案/exact pairの読取に限り、判断作成・現在preview・台帳更新やcore成功条件を持たない。

## 現在の解析モデル

| 日本語 / 英語 | 問いと利用境界 |
| --- | --- |
| 企業財務 / Company Financials | 財務状態・収益力・利益/資本と株価の比較。将来の企業価値とは別。 |
| 企業成長 / Company Growth | 実績の状態と変化。将来成長の認定ではない。 |
| 市場環境 / Market Context | 経済・市場・業種・個社の相対状態。市場判断とは別。 |
| 外部評価 / External Assessment | 外部の予想/rating/評価の比較。自分の判断に置換しない。 |
| テクニカル分析 / Technical Analysis | 価格位置・勢い・変動。急落だけを固定入口にしない。 |
| 産業動向 / Industry Trends | 母集団・期・利益basisを保つ業界比較。利益/供給能力への推測はL3。 |
| 社会動向 / Social Trends | 制度・政策・災害等の帰属/範囲を持つ主張・予定・変更。重要性/因果はL3。 |

個社モデルと市場共有subjectを分け、産業/社会の不変抽出artifactも原受領から独立させる。主張の帰属・範囲・否定・条件・精度・原文座標と記事の有限選択はObservations、比較はモデル、入力と銘柄の関連づけはGenerationが持つ。業界対応を推測せず、version付き業界・母集団・除外・出典を残す。元引用のlineage releaseと本文を探すlookup releaseを分ける。

モデル追加・生成接続・政策変更は別の変更。現政策は財務/成長/業種から経済経路を選び、他モデルを比較材料にする。モデル数や同じ価格を独立票へ増やさない。具体的な意味は[Screening](reference/screening-runtime.md)へ進む。

<a id="store-authority"></a>
## 正本と再開

クラウド正本を認可環境/Actionsの作業copyへ取得し、取得世代を照合して条件付き更新する。

| 保存単位 | 正本/寿命 |
| --- | --- |
| application DB | Research・評価/Review・提案・Selection/Operation・台帳。再生成cacheではない。 |
| native L1 | 可変headと不変body/request pack、またはsource SQLiteの原本/受領。body共有と受領記録を分ける。 |
| 共有固定入力 | 採用原本/確認/失敗のmetadata。本文はL1へ参照し、保存入力再計算に必要な間は保持する。 |
| 固定lake | L2と未移行L1のimmutable object/manifest。一つのreleaseへ固定して読む。 |
| source cache SQLite | market実行copy。未移行原本/運用状態を含み、全体は廃棄可能cacheではない。 |
| runs / 旧calibration | モデル世代/政策結果は再生成可能。保存旧run・旧較正結果は保持し、旧方式は再生成・再公開しない。 |

原本受領・原本公開・L2公開は別の成功。独立L1をL2成功待ちにせず、生成/公開は開始時の入力を固定する。未生成と結果内欠測を分け、再開で現在のモデル一覧/既定field/latest headを補わない。L3は必要断面をapplicationへ保存し、元runの存続や可変storeからの再計算に依存しない。整理/GCは原本・判断・取引履歴・R2全体の削除許可ではない。

原本や全履歴を毎日複写せず、必要投影と不変objectを再利用する。実装済みcold再生成と旧release依存を分け、source固有の取得/保持/再開は[取得元](reference/model-observations.md#source-owners)と[運用](../jobs/OPERATIONS.md#actions-write-boundary)で確認する。

[経済固定入力](../engine/src/baibai_engine/machine_analysis/generation/economic_inputs.py)も当時採用した原本と確認/失敗を固定し、後日の訂正や採用へ差替えない。冷再計算は保持原本と当時のengine/config/runtimeを要する。原本欠落は当時の欠測ではなく失敗であり、旧hash-only世代から失われたDBを復元できない。全generationのexact再生・過去市場PIT・一律のI/O上限は保証しない。実データの保持/容量/時間受入は#1820に残る。

名前・module配置と保存identityは別契約。配置変更でkey・ID・hash・意味revisionを変えず、原本bytes・受領時刻・source identityを保つ。価格日/対象期/公表日/cutoff、株式basis、訂正、null/0を混同しない。Company Financialsの現行IDは`company_financials`。移行済み公開判断の旧Selection canonicalは元hashの根拠として残し、通常readerに旧aliasを復活させない。[旧出典の読取](../jobs/OPERATIONS.md#financials-identity-cutover)を使う。

資産・運用の全体（Portfolio）はcash・取引・保有・成果、保有状態（Position/Holdings）はその一部、取引台帳（Ledger）は確認済み取引、運用成績（Portfolio Performance/Outcome）は資産全体の成績。株式の基本情報（Stocks）は保有依存ではない。ジョブ（Job）は実行/成功失敗/再開の単位、ワークフロー（Workflow）はその組立て、バッチ処理（Batch Processing）は非対話方式、スナップショット公開（Snapshot Publication）はL1/L2を含み得る固定保存物の公開。解析モデルや情報層と一対一ではない。
