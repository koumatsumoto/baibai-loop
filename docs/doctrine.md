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

Baibai Loop が回すのは 1 つの投資判断ループである。その中核技能（見積り）を実現結果と突き合わせて磨くフィードバックを、ループ自体に組み込む。

成果物の関係は[Decision flow](./domain-language.md#decision-flow)に集約する。以下は、運用の実現結果をmethod改善へ戻す方針である。

- **改善は重厚な別機構ではなく、運用に内蔵した calibration（見積りと実現の突き合わせ）で行う**。entry 時の見積り（リスクリワード・期待利回り・フェアバリュー）を実現結果（実際のリターン・利回り・valuation の収束・thesis の的中）と突き合わせ、外れた箇所（マクロの読みか、screening の閾値か、フェアバリュー推定か、耐性判定か）を一つずつ特定して見積り手法を改める。
- 突き合わせの母数は 2 系統ある。**(a) 自分の保有の実現結果**（件数は少ないが、一つひとつを長期に深く観測する。最終的な判断品質の正）と、**(b) 全銘柄の長期 horizon リプレイ計測**（過去の各時点で機械見積り・ランキングを再構成し、実現リターンと突き合わせる。見積り「手法」の較正用で、件数を桁で補う）。(b) の 3m/6m は regression alert、1y は leading evidence、production の実証的変更候補には 3y/5y の完全な evidence を必要とする（柱 5）。

<a id="improvement-value-hierarchy"></a>

### 改善提案の価値階層

基盤改善の提案と review 指摘は、次の直接的な成果により **T1 > T2 > T3 ≫ T4** の順で優先する。この階層は改善へ資源を配分する基準であり、銘柄比較における永久損失の関門や、一次情報確認・機械検算など現在の判断を正しくするための必須検証を弱めない。

| tier | 直接的な成果 | 採否の原則 |
| --- | --- | --- |
| **T1** | お買い得銘柄を拾える確率・精度を上げる。候補発見、FV / E[r] の見積り、個別 research の質を改善する | 最優先。実際の候補抽出または投資判断へ至る因果経路と、効果を確かめる計測を示す |
| **T2** | 誤った買いによる永久的な資本毀損を防ぐ。事業・財務・valuation の誤認や計算誤りを判断前に検出する | T1 に次いで優先。どの誤判断をどの検証で止めるかを示す |
| **T3** | 反復する運用の時間・費用・手戻りを減らす | T1 / T2 の作業量または判断までの時間を実際に減らせる場合に採用する |
| **T4** | 監査、再現性、復元、provenance、lineage のみを改善する | **デフォルト非採用**。人間の実損が確認された場合、または T1〜T3 への具体的で検証可能な寄与を示せる場合だけ検討する |

提案者と reviewer は、各提案・指摘の冒頭に `価値tier: Tn — <直接的な成果への因果経路>` を 1 行で宣言する。primary tier は、その提案の完了条件で直接確認する成果から 1 つ選び、副次効果で優先度を上げない。異なる tier の直接成果がそれぞれ独立した完了条件を持つ場合は提案を分ける。抽象的な「安全性」「品質」「将来役立つ」だけで T4 を上位へ読み替えない。

validation や hash のように監査にも使える手段でも、現在の候補・見積り・購入判断の誤りをその場で防ぐなら T1 または T2 である。失敗しても候補、順位、FV / E[r]、購入判断、canonical portfolio state、calibration のいずれも変えず、成果が後日の説明・追跡・復元だけに限定される場合は T4 とする。

実取引の order / fill、ledger correction、法務・規制・税務で必要な記録はこの改善優先度による削除対象ではない。T1 の改善であっても T2 の必須関門を弱める案は採用しない。

<a id="development-investment-policy"></a>

### 開発投資の大方針

改善・新機能・review 指摘へ開発資源を配分するときの判断順序。**何を達成したいか**は §1、**どの成果を優先するか**は前節の価値階層が定め、本節は **実装規模と複雑性をどう扱うか** を定める。

1. **ビジネス価値の最大化を最優先にする。** 直接の成果は前節の価値階層（T1 > T2 > T3 ≫ T4）で測る。
2. **保守性を次点に置く。** 複雑性として数えるのは概念数・結合・運用手順・維持負担であり、レビュー不能な形で持ち込まれる変更もこの負担に含める。規模はレビュー可能な単位へ分割して扱い、規模自体を棄却理由にしない。
3. **実装量と工数の大きさを棄却理由にしない。** 実装は AI が担うため、規模そのものは制約ではない。アーキテクチャが妥当でクリーンなら、実装量が大きく難しい選択肢を選んでよい。この自由は検証とレビューの省略を含まないが、検証・防御も前節の価値階層で測る — 現在の判断の誤りをその場で防ぐ T1 / T2 の検証は規模に関わらず置き、監査・再現・将来の安全のためだけの T4 の防御は置かない。
4. **同じ機能範囲を実現する 2 案では、クリーンで大きい設計を狭くて複雑な仕様より優先する。** 狭い surface へ押し込めて概念や責務境界を歪めるより、境界の通った大きい設計を選ぶ。将来の拡張性と保守性はここから生まれる。機能範囲そのものを広げる根拠にはしない — 最小で可逆な surface の選好は変わらない。
5. **実現経路を示せない提案の優先度を下げる。** 計測経路（柱 5）と、実際に読む人・使う工程を示せない機能は、規模の大小に関わらず採らない。

2 と 3 は対立しない。複雑性の評価が測るのは持ち込まれる概念・結合・運用負担であり、実装工数はその指標ではない。

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

- **(a)** 全上場銘柄の実データを持つデータ層（L1）と、決定論的なscreen・導出指標・モデル見積りを作る分析層（L2）を主軸にする。application DBの判断層（L3）はその消費者である。3層の詳細は[`architecture.md`](./architecture.md)が所有する。L2は`observed / derived / estimate`を区別し、決定論で生成してもE[r]やFV anchorを事実と呼ばない。人間とAIの解釈は`judgment`としてthesisへ置く。

  機械的機能には計測手段を持たせ、計測対象は見積り精度、実現利回り、valuationの収束など企業価値に基づく戦略が依存するものに限る。正式な計測経路は、3か月以上の長期horizonで行うestimate calibrationである。過去`as_of`のpoint-in-time状態を再構成し、見積りと実現returnを突き合わせる。3か月未満のforward-backtestによるscreen成績の最適化は行わない。

  較正リプレイでは、有意性や統計的優位を主張せず、効果量とcohort勝率で判断する。cohortの窓が重複し、独立ではないためである。仮説と採否基準は検証前に登録し、時間分割したdesignとconfirmの両方で整合する変更だけを採用する。grid searchは行わない。survivorshipとcoverageの欠けは計数で開示し、累積return、年率、シャープなどをtrack recordとして掲げない。
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

非目標は思想的なタブーではなく、**現在の戦略（企業価値の変化・1 人運用）が計測経路を持てない、または必要としない機能の線引き**である。戦略の前提が変わったら、柱 5 の計測経路を用意した上で見直してよい。

- 過去データに対する閾値の網羅探索（grid search）やパラメータ最適化、戦略の累積リターン（年率・最大ドローダウン・シャープレシオ）を実績として掲げること（誠実性の規律。柱 5）。
- 機械学習によるスコアリング・予測。スコアは軸別の座標として出し、合成点に畳まない（成分と前提を持つ機械見積り E[r] / FV アンカーは柱 5 (b) の条件下で範囲内）。
- 自動発注・リアルタイム処理。発注を人間の裁定に置くのは帰責の分業のためであり、**判断材料の生成・分析・提案の作成を AI が主導することは範囲内**。
- 銘柄全体を対象にした**短期（3 か月未満）horizon** の forward-backtest による screen 成績最適化（長期 horizon の見積り較正リプレイは柱 5 の正式な計測経路であり、非目標ではない）。
- ETF / 投資信託 / 海外株、口座・税制のモデル化。
- broker状態の自動推定、broker会計の完全複製、ledger精密化の目的化。
- 外部向けの汎用データ配信、公開MCP、書き込みAPIの提供。所有者用のread-only Web・MCPは範囲内であり、正本へのwrite権限を持たない。

## 9. 参考

構成は[architecture](./architecture.md)、資本方針は[portfolio-management](./portfolio-management.md)、操作の入口は[docs portal](./README.md#目的別の入口)を参照する。
