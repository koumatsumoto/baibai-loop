---
title: "Doctrine"
summary: "Baibai Loop の投資思想・大戦略・判断原則の正本。企業価値の変化を保守的に評価し、見積りの精度を運用の中で磨いていく単一ループを定義する。"
doc_type: doctrine
status: active
---

# Doctrine — Baibai Loop の投資思想と大戦略

この文書は、Baibai Loopが何を狙い、どの原則で判断するかを定める。domain termは[`domain-language.md`](./domain-language.md)、3層構造、package、CLI、SQLiteの契約は[`architecture.md`](./architecture.md)、資本とpositionの規律は[`portfolio-management.md`](./portfolio-management.md)、操作手順は[各skill](../.agents/skills/)が所有する。

<a id="investment-strategy"></a>

## 1. 目的と人間境界

Baibai Loopは、健全な企業の割安な機会を継続的に探し、日本個別株の現物で資本を運用する。保守的な価値評価、検証可能な企業の変化、支払価格から、上振れ・下振れの見返り、成立条件、確からしさ、実現までの時間を評価し、合理的に利益を見込める機会へ順次投資する。株価が下落した銘柄だけでなく、上昇中でも価値に対して安い銘柄を対象にする。確実な一銘柄や絶対的な底値を要求せず、不利なシナリオの存在や将来の不確実性だけで一律に見送らない。分散は機会を積み重ねた結果であり、一回の提案数、予算消化・銘柄数・投資率をノルマにしない。

日次のL1取得・L2計算を判断材料とし、AIによるL3調査・提案はユーザーの明示開始で有限対象に対して行う。AIは提案完成までを担い、人間の最終裁定とbroker操作は別工程である。自動発注、信用、借入、空売りは行わない。通常経路では既保有銘柄への追加購入を提案せず、別Stock Assessment IDによる迂回もしない。人間が実際に行った追加購入・部分売却は取引事実として記録する。

恒久的な企業価値毀損の回避を最優先とし、既存の[永久損失7軸](./reference/stock-assessment.md#permanent-loss-axes)で評価する。回復可能な赤字、下方修正、循環悪化を一律に排除せず、一時的な含み損や長い資金拘束を許容する。ただし、健全な企業であることだけで高過ぎる購入価格や回収不能なcaseを正当化しない。含み損率の固定下限は定めず、金額loss budget、年間損失停止、drawdown kill switchは置かない。

保有期間を先に固定せず、Stock Assessmentの期間はシナリオの比較窓として使い、期限売却はしない。持続可能な配当を選好するが必須にはせず、無配の価値機会も対象にする。

保有判断は重大な経済的投資理由の不成立、または残存見返り不足により`sell`を検討し、見返りが十分なら`hold`とする。不確実なら未確定とする。価格下落、経過期間、単なる回復遅延、Target到達、新規購入条件未達、他候補、集中warningの単独理由では売らない。次の候補がなくても現金を回収できる。

企業評価の`supported / uncertain / unsupported`、現在の購入条件、Proposalの`buy / hold / sell / no_action / defer`、確認済み約定を区別する。根拠付きの見送り・unknownは正常な成果である。長期資産形成のlongfolioは別目的であり、連携や銘柄重複チェックは持たない。Baibai Loopは投資助言サービスではない。

## 2. 運用モデル — 単一ループと見積りの改善

運用の実現結果を見積りの改善へ戻す。自分の保有結果と、全銘柄の過去as-ofを再構成する較正を使い分ける。成果物の関係は[Decision flow](./domain-language.md)、horizon・比較方法・採否の手順は[見積り較正](./reference/estimate-calibration.md)が所有する。

<a id="development-investment-policy"></a>

### 開発・設計・レビューの原則

1. 最優先はビジネス価値を高める戦略・機能、次に保守性・拡張性であり、技術的完全性や一般論を上位に置かない。
2. 提案は具体的な利用場面と改善効果から考え、実装・運用・理解・将来変更の負担に見合うものだけを採用する。
3. 現行仕様を正しい前提にしない。実装・実データ・実際の運用を確認し、既存の機能・状態・分岐もビジネス価値から採否を見直す。必要な価値と品質を満たす最も単純な方法を選び、短さ自体を目的にしない。
4. 自動化・共通化の前に処理自体をなくせないか考え、未確定の将来用途のために基盤・抽象化・設定・状態管理を増やさない。
5. 現行ドメインモデルの意味・責務・依存に合わせて設計する。古い構造へ継ぎ足して合わせず、重複と分岐を減らし、必要なら設計ごと置き換える。保守性・拡張性はその結果として高める。
6. 旧仕様の互換性は維持しない。不要な仕様・互換分岐と関連コード・文書・テストを一緒に削り、一度の移行や検証を恒久機構にしない。過去データとの互換性や整合性の維持を、将来のビジネス価値より優先しない。必要なら既存データを移行・再構築し、履歴をリセットしてクリーンなschemaへ置き換える。移行時に失う範囲は明示し、復元不能な本番削除は実行直前に対象を確認する。
7. 過剰な防御・監査・二重検証は追加せず、既存のものも必要性を見直して削減する。機能の正しさと必要な安全性を守る検証に絞り、実運用での発生可能性・影響と複雑さ・保守コストで判断する。推測上の稀なケースで実用化を止めず、架空の将来互換性や監査基盤を持たない。
8. 価値が不確かな大きな案は既存手段で要点を確かめるが、明らかな改善に大掛かりな実証や専用の評価基盤を要求しない。
9. レビューは名称や個別ファイルの整合だけで済ませず、実装・テスト・文書・設定・運用を横断して意味と連携を確認する。確認済み箇所や今回追加した処理も例外にしない。徹底的な調査と仕様の追加量を混同せず、所見数・テスト数・変更量を成果にしない。採否は利用実態・影響・費用で判断する。
10. この基準を提案前に適用し、再指摘を待たない。価値がなければ変更不要と結論し、判断を変える際は根拠を明示する。

## 3. ベースの考え方（5 つの柱）

各柱は **(a) 信念 / (b) 根拠 / (c) 却下した対立案** の形で記す。

### 柱 1: 事実と分析の分離

- **(a)** Stock Analysis内のobserved / derived / estimateと、人間/AIによるjudgment（Market Context Assessment・Stock Assessment）は責務を分ける。禁止表現と運用ルールは§5[事実と分析の分離](#fact-analysis-separation)を正本とする。
- **(b)** 事実と意見が混ざると、AIが過去の解釈を「事実」として再生産してしまう。観測と判断の責務・読取契約を分け、判断を参照する範囲を選べるようにして、後知恵バイアスと責任の所在の混乱を防ぐ。
- **(c)** 観測の読取へ判断を混入させず、値の由来と役割を識別する。物理配置と保存責務は[architecture](./architecture.md#store-authority)に従う。

### 柱 2: マクロは機械読み値 + material delta、構造変化は企業価値への影響として扱う

- **(a)** Market Contextは保存済みの環境観測、機械Reading、市場・業種・個別の相対状態を材料とし、AIは重要な変化と伝達経路だけを企業評価・リスクの取り方・提案へ接続する。毎回独立した旧章立てreportや主観確率を必須にしない。Market Context Assessmentの意味と明示作成は[Market Context reference](./reference/market-context.md)が所有する。環境上の含意は企業価値や機械rankingと別結果であり、乖離や攻め/守りの方向だけで自動数量・合成score・注文を決めない。対象時点・鮮度・不足を示し、環境の欠測だけで正常な他領域を止めず、重要な不足は提案の留保理由にする。
- **(b)** AIを含む技術・産業構造変化は、その仮説を外すとBase/Downside projection、FVとrequired return、永久損失、最強反対仮説、またはInvestment Proposalが変わる場合だけmaterialとする。正の影響は一次情報と必要な独立裏取りからscenario assumption、FV、比較理由へ、負の影響は`structural_decline`、最強反対仮説、必要ならscenarioとFVへ接続する。「AIを使っている」「市場が成長する」という事実だけでbase scenarioや倍率を上げない。
- **(c)** materialでない構造変化には言及・専用source・専用reviewを要求しない。AI theme、role、専用scoreでscreening、順位、sizingを変えず、企業ごとの条件付きの見返り、成立条件と永久損失リスクを同じ土俵で比較する。

### 柱 3: 見積りを磨くフィードバック先行

- **(a)** 完成した設計を待たず、不完全でもまずループを 1 周してから改善する。改善の対象は **リスクリワード、成立条件・確からしさ・所要時間の見積り精度**であり、entry 時の見積りを実現結果と突き合わせ続け、見積り手法を一つずつ改める。
- **(b)** 実際にループを回してはじめて、見積りのどこが系統的に外れているのか（マクロの読みか、フェアバリュー推定か、耐性判定か）が見えてくる。材料がなければ改善の方向は定まらない。
- **(c)** 設計を固めきってから運用を始めると、運用開始時点で陳腐化している。短期 screen の成績を大量の銘柄で backtest して最適化する重い改善ループは、企業価値と見返りを評価する戦略の改善には直結しない（柱 5）。

### 柱 4: application DB 正本、Git は method / config

- **(a)** task、Market Context Assessment、Stock Assessment・Assessment Review、Investment Proposal、portfolio ledger / outcome、Research selection・進捗を持つoperation session という application data は application DB を正本とする。再生成可能な screening run は専用 run store、market / indicator series は各 L1 store に分離する。method、設定、playbook、コード、docs は Git に置く。機械契約は DB constraint、engine 内の model、application service の write-time validation が担う。
- **(b)** 書き込みは AI との会話を入口に `baibai-engine` CLI が行う。`baibai-web` は application DB と各 read store を読むだけの UI で、閲覧専用 read model への publish も同じ読み取り側にあり、正本を書き換えない。この分業により、同じ判断や運用状態の第二の正本を作らず、CLI と UI の意味を揃えられる。
- **(c)** GitHub Issue や Markdown / YAML を application data の正本にはしない。GitHub は開発作業に使い、運用 workspace は `operation_session`、確定した entity は各 DB table に置く。データ状態の保存場所と更新元の分担は[architecture](./architecture.md#store-authority)に従う。PC上の作業コピーを使うことと、クラウドを正本とすることを区別する。

### 柱 5: 計測ファーストのデータ基盤

- **(a)** L1の取得事実から、L2の決定論的な導出・機械見積りを作り、L3の判断が消費する。層と依存は[architecture](./architecture.md)に従い、`observed / derived / estimate / judgment`を混同しない。見積り方法の優劣は[見積り較正](./reference/estimate-calibration.md)で検証し、計算・運用の不具合は正しい値と成功・失敗経路の回帰で検証する。
- **(b)** スコアは軸ごとの座標（業種相対・自己レンジ相対の percentile）であり、単一の合成点や売買指示には決して畳まない。**単位（%/年）・成分分解（reversion / carry）・前提（anchor・実現率・cap）を持つ機械見積り（E[r]・FV アンカー）は「単一の合成点」とはみなさない** — ただし (i) 出力に成分と前提を必ず併記する、(ii) 較正リプレイで予測と実現を突き合わせ続ける、(iii) 採否と投入額の判断は人間に残る、を必須条件とする。正直な軸別の事実 + 人間の判断という役割分担が、AI の強み（機械可読な事実の整理・統合）を活かしつつ、弱み（判断の責任を負えないこと）を遮断する。
- **(c)** 小標本の一人運用では機械学習スコアリングの評価不確実性が大きいため、現行戦略は固定methodと見積りcalibrationで改善する。機械学習スコアリングと外部向け汎用データ配信は採用しない。所有者の閲覧はread-only WebとCLIが担う。

<a id="vocabulary"></a>

## 4. 判断原則の適用先

### Evidence Taxonomy

企業評価のevidenceは、事業・valuation・市場から導出した観測・positioning/liquidity・catalystを区別する。これは見積り根拠を検証する観点であり、統計的なrisk factor体系ではない。マクロ経済・政策/地政学はMarket Context Assessmentの領域として扱う。厳密な保存enumはmodelを参照する。

<a id="fact-analysis-separation"></a>

## 5. 事実と分析の分離

sourceの観測、規則による導出、機械見積り、AIの判断を区別する。機械storeに因果解釈・主観的な重要度・投資判断を書き込まず、見積りを観測済み事実として扱わない。解釈・因果・予測はMarket Context AssessmentやStock Assessmentの判断として根拠とともに示す。

将来シナリオの確からしさは確認可能な根拠と反証で説明し、根拠のない精密な確率を置かない。条件付きreturnの算術やL2の機械E[r]を、企業固有シナリオの確率加重期待値と読み替えない。

言葉の有無だけで分類しない。値の由来と主張の意味を基準にし、既知の事実を記述する中立的な語まで禁止語として増やさない。

## 6. 重要な環境変化の接続

柱2のmaterialityに従い、重要な環境変化の伝達経路と反証を検討して企業評価・リスク・提案へ接続する。判断の内容は[Market Context reference](./reference/market-context.md)、明示作成は[assessment skill](../.agents/skills/market-context-assessment/SKILL.md)に従う。

## 7. 非目標

非目標は現在の戦略で必要としない機能の境界である。前提が変わった場合は[開発原則](#development-investment-policy)に従って見直し、経済仮説の有効性と計算・運用の正しさを区別して検証する。

- 過去データに対する閾値の網羅探索（grid search）やパラメータ最適化、戦略の累積リターン（年率・最大ドローダウン・シャープレシオ）を実績として掲げること（誠実性の規律。柱 5）。
- 機械学習によるスコアリング・予測。スコアは軸別の座標として出し、合成点に畳まない（成分と前提を持つ機械見積り E[r] / FV アンカーは柱 5 (b) の条件下で範囲内）。
- 自動発注・リアルタイム処理。発注を人間の裁定に置くのは帰責の分業のためであり、**判断材料の生成・分析・提案の作成を AI が主導することは範囲内**。
- 銘柄全体を対象にした**短期（3 か月未満）horizon** の forward-backtest による screen 成績最適化（長期 horizon の見積り較正リプレイは柱 5 の正式な計測経路であり、非目標ではない）。
- ETF / 投資信託 / 海外株、口座・税制のモデル化。
- broker状態の自動推定、broker会計の完全複製、ledger精密化の目的化。
- 外部向けの汎用データ配信、公開MCP、書き込みAPIの提供。所有者用のread-only Webは範囲内であり、正本へのwrite権限を持たない。
