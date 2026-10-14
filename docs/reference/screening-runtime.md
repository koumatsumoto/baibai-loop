---
title: "Screening runtime reference"
summary: "Security Analysis、current Candidate Discovery Method、Review Setの実行境界の正本。"
doc_type: reference
status: active
---

# Screening runtime

## 役割

screeningはValuationの財務/倍率計算・機械推計とMarket Contextの価格/需給結果を組み込み、全対象銘柄のobserved / derived / estimateをSecurity Analysisへ固定し、configured Valuation Approachesが生成したNominationのexact unionをReview Setにする。Review Setは機械成果物であり、投資判断ではない。Research Triageだけが各entryへ`research / skip`とresearch priorityを発行する。

機械E[r]/FV anchor、Market Context Assessment、portfolio stateは参考文脈である。Review Setのnomination、membership、orderには使わない。ER/FVの推計・保存済み照会は[Valuation metrics](./valuation-metrics.md#machine-valuation)、企業固有の条件付き評価は[Thesis](./thesis.md#scenario-arithmetic)が所有する。

<a id="rule-purpose"></a>

## ルールの目的と根拠

データが計算可能か、価値Approachが適用できるか、どこまで調査するか、購入時に何を求めるかは別の問いである。再現テストの成功は算術・契約の確認であり、経済的な閾値の最適性を意味しない。

以下の8分類は投資判断に至る各ルールの入口である。Collectionの取得失敗、[Market Contextの系列更新・Reading](./market-context.md)、[External Assessmentの観測状態](./data-sources.md#external-assessment-acquisition)による停止・欠測は、銘柄の価値判断やApproachの資格とは区別する。各境界の出所と再現性を示せても、機会損失や収益上の最適性を実測したことにはならない。未測定の影響と改善候補は[#1481](https://github.com/koumatsumoto/baibai-loop/issues/1481)で扱う。

| 種類・適用範囲 | 守る理由と限界 | 根拠の区分・正本 |
| --- | --- | --- |
| 入力scope・妥当性（L1→Security Analysis） | 普通株の指標を他のinstrumentへ適用せず、有効な価格と必要な財務・権利単位を揃える。20barは平均売買代金の入力窓であり、企業全体の投資適格性ではない | 定義・算術のcorrectness。[入力scope](#input-scope)、[指標・期間・欠測](./valuation-metrics.md)、導入[#12](https://github.com/koumatsumoto/baibai-loop/issues/12)、分離[#1518](https://github.com/koumatsumoto/baibai-loop/issues/1518) |
| 共通factと短い履歴（Candidate Discovery・比較母集団） | 必須factを共有し、未観測を0や無規制へ変えない。規模・IPO価格履歴だけで企業価値の問いを消さない | fact妥当性と規制方針。[履歴context](#history-context)。規模・182日・正FCFの一律除外は[#1481](https://github.com/koumatsumoto/baibai-loop/issues/1481)で見直した。解除は発見目的との整合であり収益優位の実証ではない |
| Approachの資格・順位（独立Nomination） | 利益、資産、再投資という異なる価値仮説に、意味の合うnative座標を使う。資格外を企業の経済的rejectへ読み替えない | [適用境界](#approach-applicability)。Asset/Reinvestmentの境界は[#1153](https://github.com/koumatsumoto/baibai-loop/issues/1153)のsemantic correction。各順位の長期優越性は別の検証課題 |
| 品質warning（runの利用判断） | 不完全なTTM・YoYを表示し、欠測を確認済み事実にしない。warning件数・比率を投資不適格判定へ転用しない | 運用上の診断。[品質context](#data-quality-context)、[#12](https://github.com/koumatsumoto/baibai-loop/issues/12)。廃止した一律YoY悪化gateの設定は[#1481](https://github.com/koumatsumoto/baibai-loop/issues/1481)で整理 |
| 探索と作業の容量（Review Set・Research Operation） | 有限の入力・作者/独立Review負荷を守る。top20、最大3件、ticker順は利回りや経済的rejectの閾値ではなく、枠を埋めるノルマでもない | 運用都合。[Nomination容量](#nomination-capacity)、[明示開始と固定対象](./analysis-operations.md#明示開始と固定対象)、[#1414](https://github.com/koumatsumoto/baibai-loop/issues/1414)。候補外の明示開始もResearchの正本で扱う |
| 機械E[r]（参考prior・較正） | 相対倍率・自己レンジ・carryによる近似の系統誤差を測る。会社の利益・倍率・分配・購入要求率を代行しない | 経済的な推計仮定。[機械算式](./valuation-metrics.md#machine-valuation)、[較正と企業判断の区別](./estimate-calibration.md#estimator-vs-decision)。係数変更には長期evidenceが必要 |
| 企業評価と要求率（Thesis→CAA/Planning） | 事業根拠から独立にBase/Downsideと期間を構成し、記録した要求率で比較する。同じリスクを利益・倍率・率へ説明なく重複控除しない | 判断・暫定政策。通常6%は最適性実証済みではない。[要求年率](../portfolio-management.md#required-return-policy)、[企業の条件付き算術](./thesis.md#scenario-arithmetic)、[#1464](https://github.com/koumatsumoto/baibai-loop/issues/1464)。契約条件からの機械算術も企業仮定の採否とは分ける |
| 現在からの増分と執行（正式提案・人間の採否） | 受取済み分配の再算入、古い期間の年率水増し、未確認cash/注文の仮定を防ぐ。集中・ADV warningと資金不足は別に扱う | 算術・事実の整合と資本方針。[評価基準日](./thesis.md#planning-only-execution-pricing)、[予約とwarning](../portfolio-management.md#reservation-and-warnings)、[資金・保有・未約定の台帳](./portfolio-ledger.md#snapshot-equations)、[#1242](https://github.com/koumatsumoto/baibai-loop/issues/1242)。broker事実は人間が確認する |

### 現行条件の横断点検（2026-10-14）

下表の件数は、2026-10-13 as-ofの保存済みrun（Security Analysis 3,680行）を現行`multi-valuation-v6`でread-only再構成した同一断面の値である。保存済みReview Setの77件とentry payloadまで一致した。件数は将来の収益・最適閾値を示さず、除外理由の件数は重複し得る。Researchの実例は既存の合成回帰による。判断が未決の条件は現行動作を維持する。

| 条件・消費先 | 区分と根拠 | 同一断面の影響・限界 | 判断・担当 |
| --- | --- | --- | --- |
| 対象市場・普通株・20価格bar → Security Analysis | 2026-10-13保存run当時の条件。市場/銘柄種と20bar計算都合を一律scopeにしていた。[母集団](#input-scope)、[`build_universe`](../../engine/src/baibai_engine/screening/universe.py) | 保存runは3,680行。20bar不足理由203件は他の除外理由と重複する歴史値。2026-10-09の保存済みmarket断面では対象普通株3,692件中12件が1〜19barで、株数fact不足により条件だけ外しても新しい共通適格候補は0件 | [#1518](https://github.com/koumatsumoto/baibai-loop/issues/1518) で有効価格1本と指標別の必要本数へ分離。機会損失の改善実証とは扱わない |
| 正の時価総額fact・価格履歴proxy・JPX flag → Candidate Discoveryと比較中央値 | データ完全性と規制方針。規模・182日・ADVの旧一律除外は解除済み。[共通条件](#history-context)、[`matches`](../../engine/src/baibai_engine/screening/rule_config.py) | 共通適格3,496件。時価総額欠測167件、規制flag該当18件、丸め後0億円0件。時価総額は整数億円に丸めてから正値判定するため、極小値の潜在的な境界は残る | 欠測を0として通さず現行維持。丸め境界は実例が出た時に再評価する [#1481](https://github.com/koumatsumoto/baibai-loop/issues/1481) |
| TTM期間330〜370日・3FY連続性 → 各指標/Normalized Approach | 数学的一貫性。異なる長さ・株式basisの利益を混ぜない。[期間仕様](./valuation-metrics.md)、[現行rules](../../method/screening/rules/) | 3FYはNormalizedの資格であり全Approach共通の最低履歴ではない。期間境界の緩和による有効候補増・誤計算はこの断面では未比較 | 算術条件を維持。履歴不足の機会損失は個別に測る [#1481](https://github.com/koumatsumoto/baibai-loop/issues/1481) |
| 4 Approachのnative資格/順位 → Nomination | 投資仮説と入力妥当性。利益・3FY・非金融資産・再投資を同一指標で代用しない。[適用境界](#approach-applicability)、[実装](../../engine/src/baibai_engine/screening/discovery/review_set.py) | 各20件、重複tickerは3件で、和集合77件。機械E[r]は推薦に使わない。順位の経済的優位は未実証 | 意味上の除外は維持。アプローチ別の発見・見送り理由を検証する [#1481](https://github.com/koumatsumoto/baibai-loop/issues/1481) |
| 各Approach top20・Triage最大80件 → Review Set/AI入力 | 運用予算。[Nomination容量](#nomination-capacity)、[Triage入力型](../../batch/src/baibai_batch/analysis/models.py) | 深さ20で77件。保存済み指標のまま21へ広げると81件（+4）となり、入力上限80件を超える。rank21を経済的rejectとは扱えない | 20を維持。容量を変えるならTriage負荷・上限と発見価値を一緒に判断する [#1481](https://github.com/koumatsumoto/baibai-loop/issues/1481) |
| 業種中央値の母数10・市場fallback → relative指標、Normalized/Reinvestment | データ完全性を守る慣例で、経済的最適値ではない。[中央値の母数](./valuation-metrics.md#median-population-rationale)、[実装](../../engine/src/baibai_engine/valuation/metrics/relative.py) | 現行選出のNormalized/Reinvestmentに母数10未満の業種は0。保存指標を固定し、両者のfloorだけを5/20にした**部分感度**では和集合が各78/77件、入替はReinvestmentに発生。全指標再計算・成果比較ではない | 10を暫定維持。fallback基準別の発見・品質・成果を時点固定で追加検証する [#1481](https://github.com/koumatsumoto/baibai-loop/issues/1481) |
| 品質warning → runの利用判断 | データ完全性の診断。TTM 5%/20件、YoY欠測10%は企業のhard exclusionではない。[品質context](#data-quality-context)、[現行rules](../../method/screening/rules/) | 選出件数へ直接のgate影響はない。実運用でwarningが無視・誤読される頻度は未測定 | 警告として維持。停止条件へ昇格する根拠が出た場合のみ再検討する [#1481](https://github.com/koumatsumoto/baibai-loop/issues/1481) |
| Triage後の明示選択・Research最大3対象 → Operation | 運用予算。Triageの`research/skip`と購入資格は異なる。[Research進行](./analysis-operations.md#明示開始と固定対象)、[対象型](../../engine/src/baibai_engine/research/operation/progress.py) | 77候補を一度にResearchしない上限。rankや欠測だけで候補外を経済的rejectにせず、Review Set外の明示開始も可能 | 最大3を維持。作業量と見落としを実運用で測る [#1481](https://github.com/koumatsumoto/baibai-loop/issues/1481) |
| 最新Reviewed Thesis・同日の評価・将来分配/期間 → CAA/Planning/Position Review | 数学的一貫性と運用負担。受取済み・権利確定済みcashの二重算入を防ぐ。[評価基準日](./thesis.md#planning-only-execution-pricing)、[購入条件](../../engine/src/baibai_engine/research/entry_policy.py) | 既存回帰では夜のallocateが翌朝`valuation_basis_requires_refresh`となる。差分scaffoldは元資料・予測を保持できるが、新しい独立Reviewが必要。無イベントの翌日を安全に自動評価できる根拠は未確認 | 日付だけの付替えをせず現行維持。将来cashの権利・残期間・企業変化を同一時点で表す最小契約を検証する [#1481](https://github.com/koumatsumoto/baibai-loop/issues/1481) |
| 7暦日以内の正のquote・権利basis → CAA/Planning/Position Review | データ完全性。前営業日quoteを許し、古値・権利差を誤用しない。[購入条件](../../engine/src/baibai_engine/research/entry_policy.py)、[保有判断](./position-review.md) | 合成回帰で3日上限は保有提案null、7日上限はhold。休日とsource更新遅延を区別した実運用影響は未測定 | 7日を暫定維持。休日境界と更新停止時の鮮度・提案品質を検証する [#1481](https://github.com/koumatsumoto/baibai-loop/issues/1481) |
| 既保有・active予約・同CAA買約定 → 新規entry | 投資方針と重複執行防止を別理由で判定。[購入条件](../../engine/src/baibai_engine/research/entry_policy.py)、[資本方針](../portfolio-management.md#capital-guidance) | 現行はいずれもdefer。既保有の一律禁止による追加機会損失は実測していない。予約・同CAA約定は別Thesisでも二重提案を防ぐ | 既保有条件の採否は未決。限界資本で新規候補と比較するfixtureを作り、親判断まで維持する [#1481](https://github.com/koumatsumoto/baibai-loop/issues/1481) |
| 100株単元・20〜30万円guide・cash、Pmax/要求率6% → CAA/Planning | 算術・資本方針。`floor(cash/単元額)`と`max(1,floor(guide/単元額))`の小さい方。集中/ADVはwarning。[資金目安](../portfolio-management.md#capital-guidance)、[購入条件](../../engine/src/baibai_engine/research/entry_policy.py) | 合成回帰の1,000円・cash50万円・guide30万円は300株。1単元がguide超でもcash内なら1単元とwarning。実portfolioの損益効果は未測定 | 単元・cashのhard制約とwarningを維持。guideや集中閾値の経済的効果は別評価し、要求率6%の既済判断は[#1464](https://github.com/koumatsumoto/baibai-loop/issues/1464)、残る配分方針は[#1481](https://github.com/koumatsumoto/baibai-loop/issues/1481) |

## Public CLI

screening runtimeの公開commandは`baibai-engine screening --help`から辿る。本書で扱う
`run`、`review-set publish / show`、`prune`のoptionのrequired / default / choiceと出力形式は各commandのpublic
`--help`を正本とする。日常運用の実行順と停止条件は
[`research-triage` skill](../../.agents/skills/research-triage/SKILL.md)と
[`batch/OPERATIONS.md`](../../batch/OPERATIONS.md)が所有する。

通常の`run`はSecurity AnalysisとReview Setを先に計算・検証し、同じSQLite connectionの一つのtransactionでrun・全子データ・Review Setを保存する。出力には両IDを含む。Review Setの挿入失敗は新runを含めてrollbackする。paired Review SetのIDとcreated_atはimmutable runのIDとrun_atに固定し、同じrunの再試行で新しい集合を作らない。

`run`のexit 2は両publication済みのpartial warningである。warningを確認してから出力の両IDで後続へ進む。明示的`review-set publish`は保存済みrunを対象にする入口として残すが、通常dailyは再発行しない。Review Setの再表示に再発行を使わない。`--run-revision-id` + `--run-at`と`--review-set-id` + `--created-at`はcallerがidentityを固定する場合のsame-ID idempotencyを提供する。daily runnerは実行場所にかかわらずserver-generated identityを使い、AI outputからIDやclockを受け取らない。

## Security Analysis

run storeはrun identityと全銘柄のSecurity Analysis、source runに束縛した
再現可能なReview Setを保持する。field、table、schema versionはengine modelとDB
schemaを正本とする。

Security Analysisは`observed / derived / estimate`を混同しない。欠損を0へ補完せず、解釈・因果・売買判断を書かない。

## Candidate Discovery

<a id="input-scope"></a>

Security Analysisは普通株・対象市場・as-of以前の有効価格barが1本以上ある銘柄を評価対象とする。価格barが0本、最新終値またはas-of基準の最新価格が非正・非有限な入力は評価対象にしない。sourceの分割factor `0` はイベントなしを示し、終値以前・当日・以後のどの位置でも銘柄除外の理由にしない。factorを適用するのは価格日より後・as-of以前のイベントだけである。平均売買代金は20barすべての売買代金が揃う場合だけ計算し、足りなければnullにする。株数・現在価格・分割basisの欠測を補完して候補にしない。`listing_span_days`は最初の利用可能barからの期間proxyであり正式な上場日ではない。`price_history_sessions_750d`と`price_history_coverage_750d`は短い履歴・疎な履歴の品質contextとして残す。cache coverageのrequired-field 75%とbar・財務密度は、as-of masterの対象市場かつ東証33業種の普通株を母集団とし、価格・財務が欠けた普通株も分母に残す。対象市場の未知業種は分類を更新するまで停止する。各指標の入力期間・TTM・株式basisは[Valuation metrics](./valuation-metrics.md)に従い、productionと較正replayで共有する。入力不足はnullや品質contextとして残し、実績・予測・観測期間を混ぜて穴埋めしない。

<a id="history-context"></a>

共通eligibilityは正の有限な時価総額fact、非負で有限な価格barの期間、JPX flag、これら必須factの有無を扱う。現行rulesの時価総額・bar期間floorは0で、100億円未満や182日未満も候補発見と業種・市場の比較母集団に含める。bar期間は最古の利用可能な価格barからの日数proxyで、実上場日や財務履歴ではない。短い履歴、サイズ、ADVはcontextとしてSecurity Analysis、Review Set、Research Triage、Research入力へ残し、各指標に必要な履歴・財務入力とJPX制約は保つ。ADVは値が低い場合も欠損時も除外に使わない。その後、各approachが独立にnominateする。採用理由は[#1481](https://github.com/koumatsumoto/baibai-loop/issues/1481)、現行値は[screening rules](../../method/screening/rules/)、完全なeligibility・並び順・method hashは[Review Set実装](../../engine/src/baibai_engine/screening/discovery/review_set.py)を正本とする。

<a id="approach-applicability"></a>

| valuation approach | 主座標・適用する理由 |
| --- | --- |
| `current-earnings-power` | 正のcurrent PERとsector gapで、現在利益の持続性と業種割安の理由を問う。CFは従キーと裏付けのcontextで、一過性利益はResearchで反証する |
| `normalized-earnings-power` | 3FY normalized PERのsector gapで、一期間の異常から離れた利益力を問う。株式basisと連続FYを揃え、事業構造の変化はResearchで確認する。[先行測定](../../reports/studies/2026-08-02-normalized-per-production/report.md)があるが、3FYを全社共通の調査資格にはしない |
| `asset-value` | 正のasset-backed ratioから、株主へ実現する資産の拘束・換金可能性を問う。非金融のasset proxyを金融資産・資金調達そのものへ適用しない |
| `reinvestment-value` | 割安なsector-relative P/Sと相対的な収益性で、売上成長が価値へ変わるかを問う。FCFの符号だけで投資回収や資金余力を認定しない |

Asset Valueは正のasset-backed ratioを持つ非金融企業を対象とする。金融業ではcash、debt、securitiesが事業上のasset / fundingそのものなので、非金融企業向けgross asset proxyを残余価値として適用しない。net cashは比較文脈として残し、候補の採否には使わない。

Reinvestment Valueはsector-relative P/Sの割安さと、利益率・capital returnの質を組み合わせる。sector母数が薄い場合はmarket medianを使う。capital returnは`operating_profit_ttm / (same_state_equity_yen + debt - cash)`で、自己資本はJ-Quantsの同一実績行、debt/cashは選択したEDINET recordに基づく。両sourceのBS日一致を保証する厳密ROICではない。TTM営業利益が作れなければ累計利益の年率換算や別の利益で補わない。株式・会計basisは[valuation metrics](./valuation-metrics.md#51-資本の分母)に従う。

FCF yieldは有限な既知値を必須とし、0・負値も推薦対象にする。既存の利益率・capital-return floorは維持し、FCFは順位の末尾の従キーで高い順に比較する。負FCFを成長投資と認定せず、欠測を0へ補完しない。ResearchはCFのsource・品質・期間、維持投資/成長投資・運転資金・回収・資金余力を確認する。半期の`approximated`値や書類metadataの年度末を、通年実績CFや測定期間へ読み替えない。

ReinvestmentのNomination条件と、Review SetからTriageへ渡す観測は分ける。他Approachで推薦された銘柄も、既知のP/S gap・売上YoY・FCF yield・利益率・capital-return proxyを項目ごとに保持し、負値や0を欠測へ変えない。比率は必要入力が揃い分母が正の場合だけ計算し、計算不能な項目はnullとする。過去の保存snapshotがグループ全体をnullとしていた場合、その欠落を現在値で補わない。

primary coordinateがnull、非有限、またはapproachの要件を満たさない銘柄は、そのapproachでnominateしない。金融sector除外、quality threshold、median population / fallback、従キー、tickerまでmethod hashに含め、同じSecurity Analysisとrulesから同じ結果を再構成する。

## Review Set

<a id="nomination-capacity"></a>

各Approach top20は有限の発見・Triage入力枠で、rank21以下を経済的rejectにしない。容量の変更は調査負荷と失う機会を比較し、推薦件数や取引件数の増加自体を成果にしない。企業評価の適用範囲や正式Researchの開始とは別の契約である。

各Valuation ApproachのNominationをtickerで統合し、重複tickerは1entryへまとめ、全Nominationを保持する。各Approachのconfigured nomination depthに従う集合のunionであり、採用Approachとdepthは[現行rules](../../method/screening/rules/)を参照する。ticker昇順はbyte-equivalentなserializationのためだけに使い、global rankやresearch priorityを意味しない。

publisherはsource run、as-of、rules hash、method hash、全Security Analysisからpayloadを再計算し、不一致を拒否する。published rootは`screening_rules_hash`をprovenanceとして持つ。Review Setはapplication DBやResearch Triageを読まず、同じrun・rules・implementationから同じNomination unionを再構築するL2である。各entryはidentity、`nominations`、grouped `analysis`だけを持つ。`analysis.expected_return`はsecondary machine priorのsnapshotであり、membershipを持たない。

旧rulesの指定は設定値の指定であり、過去の算式を凍結しない。再実行は現行算式を含むmethod hashを持つため、method IDだけで旧methodの再現と扱わない。保存済みrun・Review Setのexact読取は原hashとNominationを保持し、現行算式で再計算しない。
[#1518](https://github.com/koumatsumoto/baibai-loop/issues/1518)の1bar scopeは`UNIVERSE_SCOPE_REVISION`でproductionの`screening_rules_hash`、Review Setの`method_hash`、calibration panelの`rules_hash`に区別して含める。`multi-valuation-v6`という表示名や既存rules fileだけでは旧20bar母集団との一致を認めない。旧E[r]較正contextは新runのrules hashと一致せず非表示になり、旧Triageは元のReview Set/run/hashへの束縛を保つ。新定義で旧artifactのhashだけを差し替えない。

Normalized Earnings Powerのnative eligibility/orderは`normalized_per_3fy`と同sector gapを使う。FV/E[r] estimatorは`normalized_per_3fy`を入力にしない。

MCPは保存済みcanonical Review Setをread-onlyで参照できる。これはscreening結果の検証用read boundaryであり、Review Set membership、Nomination、Research Triage、Research Setを変更するauthorityを持たない。tool契約は[Owner MCP](../../tools/owner_mcp/README.md#triage前のreview-set)を参照する。

<a id="data-quality-context"></a>

## 品質context

TTM非exactと業績YoY欠測の件数・比率はrunのpartial warningを決める。書込済み成果物の品質を説明するもので、会社を一律除外するgateではない。指標をNominationへ使えるかは各Approachの必要入力で決める。旧`yoy_deterioration_threshold`は現行rulesから除き、較正の業績変化による[診断群](./estimate-calibration.md#diagnostic-thresholds)とは分ける。

## Research Triage

ScreeningはL2のReview Setを発行し、Researchが全entryを判断する。Triageの保存契約・CAS・入力参照は[Research Triageの契約](./analysis-operations.md#research-triage-contract)、操作は[Research Triage skill](../../.agents/skills/research-triage/SKILL.md)が所有する。純粋なCandidate型と`PublishedReviewSet`は`screening/discovery/contracts`、Nominationとunionの生成は`screening/discovery/review_set`に分ける。package initから計算をeager importしない。

## Daily batchとlocal analysis

daily runnerはcloud scheduleまたは明示的なlocal実行でL1 / L2 machine処理だけを行い、同じcanonical runs storeを進める。

```text
screening run (run + Review Set atomic commit) + independent indicators
    -> web materialize once -> existing publish path
```

Screeningの必要入力不足・実行失敗から指標refreshを独立させ、日本休日はScreeningだけをskipする。検証してcommitしたL1 chunkは後続失敗でも保持する。公開可能なstoreから全viewsを一度生成し、Screening未更新時はhistoryの生成/uploadとrun pruneを省く。現在画面とmetaは同じoperative runを参照し、過去の単独run・直接ID照会・cutoff付きValuationは引き続き読める。物理store/schemaやcron頻度を変えない。

cloudからL3 Research Triageを発行しない。local `baibai-batch analysis run`はpull済みruns storeから対象`as_of`のlatest published Review Setを読み、必要な場合だけReview Set全体を原則1 requestで分類してResearch Triageを発行する。対象日にReview Setが無ければ前営業日へfallbackしない。有限選択とResearchは明示ユーザー開始で進め、提案完成と最終採否は分ける。旧章立てreport、migration、全期間再取得はどちらの日次経路にも載せない。

## Pruneと履歴

`prune --keep N`は新しいrunをN世代残し、子のSecurity AnalysisとReview Setを同一transactionで削除して`VACUUM`する。Research Triage、thesis、Capital Allocation Assessment、Position Reviewはapplication DBのcanonical judgmentなのでrun pruneから独立する。

市場・業種・個別相対の算術とcutoffは[Market Context読取](./market-context.md#market-context-reads)が所有する。Screeningは業種平均対市場平均に既存の比較母集団を渡し、ValuationのER/FVや4 Approachの推薦式へ環境scoreを追加しない。

## Market store inputs

価格、財務、銘柄master、calendar、信用・空売り、JPX規制、EDINET、上場廃止・現金公開買付けとcoverageを読む。現行table/columnはschema、lake datasetは`baibai-engine lake inventory`を参照する。

値の意味は[valuation metrics](./valuation-metrics.md)、sourceの選択は[data sources](./data-sources.md)、PIT・coverage・hydrateは[market lake](./market-lake.md)に従う。

JPX決算calendarは銘柄ごとの次回決算日の注記だが、snapshotの取得失敗は現行`run`全体を保存前に止める。個別銘柄にcalendar行がないことと同じ停止条件ではない。予定日当日・経過後の状態は`scheduled_date_reached`であり、発表確認を意味しない。保存済みの旧`announced`もWeb APIの投影とResearch入力では同じ意味として扱い、exact読取と元snapshotは保全する（[#1507](https://github.com/koumatsumoto/baibai-loop/issues/1507)）。EDINETのCSV抽出にhard failureが1件でもあると前処理snapshotを書かず、dailyもScreeningを進めない。いずれも取得・成果物の完全性に関する現行運用境界であり、銘柄の経済的な除外根拠や停止による機会損失の実測ではない。

J-Quantsのread取得は[provider](../../engine/src/baibai_engine/market/providers/jquants.py)が生成するClientV2のHTTPS adapterで再試行を所有する。単一GETは初回＋最大5回で、429と既存の一時HTTP失敗を対象にする。有効な`Retry-After`の秒数／HTTP-dateを優先し、無いか不正なら30・60・120・300・600秒で待つ。認証・403・解析失敗を再試行せず、SDKのページ・日付range全体も再実行しない。従来の成功chunk保存・cacheからの再開を維持する。job残時間をproviderへ渡す経路は現行に無く、残時間による待機前終了は未実装である。永続cooldownや他providerのretryはこの契約に含めない。

## 検証

- rules/method hash一致
- 同一入力でReview Setがbyte-equivalent
- E[r]だけを変更してもmembership/order不変
- Research Triageの全entry一致、priority、rules/method identity、expected prior ID
- 対象日のReview Setなし、空Review Set、exact既存Triageでmodel process 0。active Operationはdaily Triageを止めない
- 通常Review Setは共有Market Context Assessment projectionを1回だけ含む1 AI request、invalid resultはcanonical write 0
- Triage publishではOperation 0。ユーザーの明示開始と固定した新規/保有集合（0件も含む）でOperation 1
- run storeとapplication DBがcurrent schemaに一致
- Web/APIがReview Set、Research Triage、Capital Allocation Assessmentを同じbindingで表示
