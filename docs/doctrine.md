---
title: "Doctrine"
summary: "Baibai Loop の投資思想・大戦略・判断原則の正本。企業価値の変化を保守的に評価し、見積りの精度を運用の中で磨いていく単一ループを定義する。"
doc_type: doctrine
status: active
---

# Doctrine — Baibai Loop の投資思想と大戦略

この文書は、Baibai Loopが何を狙い、どの原則で判断するかを定める。domain termは[`domain-language.md`](./domain-language.md)、3層構造、package、CLI、SQLiteの契約は[`architecture.md`](./architecture.md)、資本とpositionの規律は[`portfolio-management.md`](./portfolio-management.md)、操作手順は[各skill](../.agents/skills/)が所有する。

## 1. 目的と人間境界

Baibai Loopは、保守的な価値評価、検証可能な企業の変化、支払価格を組み合わせ、日本個別株の現物で資本を運用する。株価が下落した銘柄だけでなく、上昇中でも価値に対して安い銘柄を対象にする。予算消化・銘柄数・投資率をノルマにしない。

AIは調査と提案、人間は最終裁定とbroker操作を担う。自動発注、信用、借入、空売りは行わない。通常経路では既保有銘柄への追加購入を提案せず、別Thesis IDによる迂回もしない。人間が実際に行った追加購入・部分売却は取引事実として記録する。

3〜12か月を主戦場とするが期限売却はしない。持続可能な有配を選好し、強い価値機会なら無配も許容する。回復可能な赤字、下方修正、循環悪化を一律に排除せず、資金繰り・負債返済・希薄化・恒久的価値毀損を評価する。金額loss budget、年間損失停止、drawdown kill switchは置かない。

保有判断は重大な経済的投資理由の不成立、または残存見返り不足により`exit`を検討し、見返りが十分なら`hold`とする。不確実なら未確定とする。価格下落、経過期間、単なる回復遅延、Target到達、新規購入条件未達、他候補、集中warningの単独理由では売らない。次の候補がなくても現金を回収できる。

企業評価の`candidate / defer / reject`、現在の購入条件、CAAの`allocate / no_allocation / defer`、確認済み約定を区別する。根拠付きの見送り・unknownは正常な成果である。長期資産形成のlongfolioは別目的であり、連携や銘柄重複チェックは持たない。Baibai Loopは投資助言サービスではない。

## 2. 運用モデル — 単一ループと見積りの改善

運用の実現結果を見積りの改善へ戻す。自分の保有結果と、全銘柄の過去as-ofを再構成する較正を使い分ける。成果物の関係は[Decision flow](./domain-language.md)、horizon・比較方法・採否の手順は[見積り較正](./reference/estimate-calibration.md)が所有する。

<a id="development-investment-policy"></a>

### 開発・設計・レビューの原則

1. 最優先はビジネス価値を高める戦略・機能、次に保守性・拡張性であり、技術的完全性や一般論を上位に置かない。
2. 提案は具体的な利用場面と改善効果から考え、実装・運用・理解・将来変更の負担に見合うものだけを採用する。
3. 現行実装・実データ・実際の運用を確認し、必要な価値と品質を満たす最も単純な方法を選ぶ。短さ自体を目的にしない。
4. 自動化・共通化の前に処理自体をなくせないか考え、未確定の将来用途のために基盤・抽象化・設定・状態管理を増やさない。
5. 保守性・拡張性は責務と依存を明確にし、重複と分岐を減らして高める。既存設計への継ぎ足しが複雑なら置き換える。
6. 旧仕様の互換維持は原則不要。不要な仕様と関連コード・文書・テストを削り、一度の移行や検証を恒久機構にしない。
7. 機能の正しさと必要な安全性を守る検証は行うが、「念のため」の防御・監査・承認・証跡や、理論上の例外対応を増やさない。
8. 価値が不確かな大きな案は既存手段で要点を確かめるが、明らかな改善に大掛かりな実証や専用の評価基盤を要求しない。
9. 徹底的な調査と仕様の追加量を混同せず、所見数・テスト数・変更量を成果にしない。採否は利用実態・影響・費用で判断する。
10. この基準を提案前に適用し、再指摘を待たない。価値がなければ変更不要と結論し、判断を変える際は根拠を明示する。

## 3. ベースの考え方（5 つの柱）

各柱は **(a) 信念 / (b) 根拠 / (c) 却下した対立案** の形で記す。

### 柱 1: 事実と分析の分離

- **(a)** Security Analysis内のobserved / derived / estimateと、人間/AIによるjudgment（macro context・thesis）は責務を分ける。禁止表現と運用ルールは§6[事実と分析の分離](#fact-analysis-separation)を正本とする。
- **(b)** 事実と意見が混ざると、AI が過去の解釈を「事実」として再生産してしまう。store・table単位で分けておけば「judgmentを AI に見せない」という選択ができ、後知恵バイアスと責任の所在の混乱を防げる。
- **(c)** 同一tableに`type`列やflagでjudgmentを混在させる案は、混入したときに見落としやすく機械チェックも利きにくい。store・table単位の物理的な分離が最も安全。

### 柱 2: マクロは機械読み値 + material delta、構造変化は企業価値への影響として扱う

- **(a)** マクロは次の2層に分ける。
  - **macro reading（L2）**：登録全系列の水準、方向、percentile、閾値注記、観測の齢を毎営業日に機械計算する。regime分類、合成score、売買signalは出さない。
  - **macro context（L3）**：人間が判断するときだけ書く。環境評価（core）、経路横断の支配的な力と相互作用（synthesis）、日本株の調査・資本配分への接続（connection）を分け、coreとsynthesisはuse-case agnosticにする。synthesisとconnectionが引用できるseriesは、依拠するcore sectionが引用済みのものに限り、そのsectionを`core_section_ids`で名指しする。この参照方向は機械契約で強制し、coreを単独で自己完結させる。coreは攻め／守りのどちらの環境かを反証条件付きで判断し、sector tilt、research優先度、sizing cautionはconnectionだけに置く。field単位の契約は[`reference/macro.md`](./reference/macro.md#3-層構成core環境評価synthesis統合評価connection積立ループ接続)が所有する。

  どちらも機械screening、ranking、sizingへ混入させず、売買タイミング、現金比率、配分を指示しない。macro contextはresearchの着手順を決めるjudgment入力として使う。contextがない、または古くても候補抽出は続け、未来情報だけをhard errorにする。鮮度は書き手が賞味期限を宣言せず、読み手が`as_of`と自分の閾値で判断する。
- **(b)** AIを含む技術・産業構造変化は、その仮説を外すとBase/Downside projection、FVとrequired return、永久損失、最強反対仮説、またはCapital Allocation Assessmentが変わる場合だけmaterialとする。正の影響は一次情報と必要な独立裏取りからscenario assumption、FV、比較理由へ、負の影響は`structural_decline`、最強反対仮説、必要ならscenarioとFVへ接続する。「AIを使っている」「市場が成長する」という事実だけでbase scenarioや倍率を上げない。
- **(c)** materialでない構造変化には言及・専用source・専用reviewを要求しない。AI theme、role、専用scoreでscreening、順位、sizingを変えず、企業ごとのE[r]と永久損失リスクを同じ土俵で比較する。

### 柱 3: 見積りを磨くフィードバック先行

- **(a)** 完成した設計を待たず、不完全でもまずループを 1 周してから改善する。改善の対象は **リスクリワードと期待利回りの見積り精度**であり、entry 時の見積りを実現結果と突き合わせ続け、見積り手法を一つずつ改める。
- **(b)** 実際にループを回してはじめて、見積りのどこが系統的に外れているのか（マクロの読みか、フェアバリュー推定か、耐性判定か）が見えてくる。材料がなければ改善の方向は定まらない。
- **(c)** 設計を固めきってから運用を始めると、運用開始時点で陳腐化している。短期 screen の成績を大量の銘柄で backtest して最適化する重い改善ループは、企業価値と見返りを評価する戦略の改善には直結しない（柱 5）。

### 柱 4: application DB 正本、Git は method / config

- **(a)** task、macro context、research_triage、research、Capital Allocation Assessment、portfolio ledger / outcome、operation session という application data は application DB を正本とする。再生成可能な screening run は専用 run store、market / macro series は各 L1 store に分離する。method、設定、playbook、コード、docs は Git に置く。機械契約は DB constraint、engine 内の model、application service の write-time validation が担う。
- **(b)** 書き込みは AI との会話を入口に `baibai-engine` CLI が行う。`baibai-web` は application DB と各 read store を読むだけの UI で、閲覧専用 read model への publish も同じ読み取り側にあり、正本を書き換えない。この分業により、同じ判断や運用状態の第二の正本を作らず、CLI と UI の意味を揃えられる。
- **(c)** GitHub Issue や Markdown / YAML を application data の正本にはしない。GitHub は開発作業に使い、運用 workspace は `operation_session`、確定した entity は各 DB table に置く。外部 SaaS を正本にすると local-first の運用と application service の境界が崩れるため採用しない。

### 柱 5: 計測ファーストのデータ基盤

- **(a)** L1の取得事実から、L2の決定論的な導出・機械見積りを作り、L3の判断が消費する。層と依存は[architecture](./architecture.md)に従い、`observed / derived / estimate / judgment`を混同しない。見積り方法の優劣は[見積り較正](./reference/estimate-calibration.md)で検証し、計算・運用の不具合は正しい値と成功・失敗経路の回帰で検証する。
- **(b)** スコアは軸ごとの座標（業種相対・自己レンジ相対の percentile）であり、単一の合成点や売買指示には決して畳まない。**単位（%/年）・成分分解（reversion / carry）・前提（anchor・実現率・cap）を持つ機械見積り（E[r]・FV アンカー）は「単一の合成点」とはみなさない** — ただし (i) 出力に成分と前提を必ず併記する、(ii) 較正リプレイで予測と実現を突き合わせ続ける、(iii) 採否と投入額の判断は人間に残る、を必須条件とする。正直な軸別の事実 + 人間の判断という役割分担が、AI の強み（機械可読な事実の整理・統合）を活かしつつ、弱み（判断の責任を負えないこと）を遮断する。
- **(c)** 小標本の一人運用では機械学習スコアリングの評価不確実性が大きいため、現行戦略は固定methodと見積りcalibrationで改善する。機械学習スコアリングと外部向け汎用データ配信は採用しない。所有者専用のread-only MCPは、外部向けサービスとは区別する。

<a id="vocabulary"></a>

## 4. 判断原則の適用先

正準用語、命名と成果物の関係は[domain-language](./domain-language.md)、責務・依存・storeの正本は[architecture](./architecture.md)が所有する。

### Evidence Taxonomy

企業評価のevidenceは、事業・valuation・市場から導出した観測・positioning/liquidity・catalystを区別する。これは見積り根拠を検証する観点であり、統計的なrisk factor体系ではない。macro・policy/geopoliticalはMacro Contextの領域として扱う。厳密な保存enumはmodelを参照する。

## 5. 責務境界

操作の順序と人間確認は[各skill](../.agents/skills/)、artifact固有の意味は[reference](./reference/README.md)が所有する。

<a id="fact-analysis-separation"></a>

## 6. 事実と分析の分離

sourceの観測、規則による導出、機械見積り、AIの判断を区別する。機械storeに因果解釈・主観的な重要度・投資判断を書き込まず、見積りを観測済み事実として扱わない。解釈・因果・予測はMacro ContextやThesisの判断として根拠とともに示す。

言葉の有無だけで分類しない。値の由来と主張の意味を基準にし、既知の事実を記述する中立的な語まで禁止語として増やさない。

## 7. 分析階層：世界情勢 → 地域経済 → 個別資産

Macro Contextは世界情勢、日本経済、個別資産への含意の順で整理する。これは分析の構成であり、因果が常に一方向へ流れるという仮定ではない。伝達経路と反証は[macro reference](./reference/macro.md)に従って検討する。

## 8. 非目標

非目標は現在の戦略で必要としない機能の境界である。前提が変わった場合は[開発原則](#development-investment-policy)に従って見直し、経済仮説の有効性と計算・運用の正しさを区別して検証する。

- 過去データに対する閾値の網羅探索（grid search）やパラメータ最適化、戦略の累積リターン（年率・最大ドローダウン・シャープレシオ）を実績として掲げること（誠実性の規律。柱 5）。
- 機械学習によるスコアリング・予測。スコアは軸別の座標として出し、合成点に畳まない（成分と前提を持つ機械見積り E[r] / FV アンカーは柱 5 (b) の条件下で範囲内）。
- 自動発注・リアルタイム処理。発注を人間の裁定に置くのは帰責の分業のためであり、**判断材料の生成・分析・提案の作成を AI が主導することは範囲内**。
- 銘柄全体を対象にした**短期（3 か月未満）horizon** の forward-backtest による screen 成績最適化（長期 horizon の見積り較正リプレイは柱 5 の正式な計測経路であり、非目標ではない）。
- ETF / 投資信託 / 海外株、口座・税制のモデル化。
- broker状態の自動推定、broker会計の完全複製、ledger精密化の目的化。
- 外部向けの汎用データ配信、公開MCP、書き込みAPIの提供。所有者用のread-only Web・MCPは範囲内であり、正本へのwrite権限を持たない。

## 9. 参考

構成は[architecture](./architecture.md)、資本方針は[portfolio-management](./portfolio-management.md)、操作の入口は[docs portal](./README.md#目的別の入口)を参照する。
