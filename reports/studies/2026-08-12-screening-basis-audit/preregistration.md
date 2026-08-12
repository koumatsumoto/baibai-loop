---
title: "screening基盤の反証監査 — 事前登録"
summary: "現行screeningの株式・期間・母集団・時点・合成basisを全件とtailで反証し、明示contractに反する到達可能な欠陥だけを修正する。"
doc_type: measurement-record
status: active
date: 2026-08-12
---

# screening基盤の反証監査 — 事前登録

価値tier: T2 — basisの混同や欠損時のfail-openで割安に見える銘柄を候補へ通す経路を塞ぎ、永久損失候補を一次researchへ接続する確率を下げる。

## 1. 目的、固定時点、非盲検性

本監査は #941 に対し、現行screeningの入力からselectionまでを壊す向きで調べる。新しいfactorの探索や短期成績の最適化は行わない。確認するのは、現在宣言している式・rule・時点契約が実データと実装で一致し、欠損やcorporate actionのときに別basisを同じ量として扱っていないことである。

事前登録のrepository baselineは`ec86eaebc8a382257206d6b2bec4dfed3b637023`、production as-ofは`2026-08-12`とする。市場store、screening store、application storeは計測開始時のidentity、schema version、最大日付、行数を結果artifactへ固定する。current cross-sectionに加え、storeに存在する全financial summary、全corporate-action row、production calibration storeの全cohortを対象にし、結果を見て期間や銘柄を選び直さない。

実装の静的読解からfailure modeの候補は既知であり、完全なblind studyではない。特に次のコード形状は事前に観測した。

- `cashflow-yield-discount`はconfig上`fcf_yield_required_positive: true`だが、現行predicateはFCFが存在するときだけ非正を拒否する。
- 発行済株式と自己株式はfieldごとにcarry forwardされ、source日が一致するとは限らない。
- `deterioration_gate_unmeasurable`は3種類のYoYがすべて欠損したときだけ真になる一方、一部playbookは`operating_profit_yoy`だけを劣化判定に使う。
- universeの時価総額と平均売買代金はselection predicateより前に丸められる。
- dividend yieldとnet share changeの欠損は、現行E[r]の加算項で0として扱われる。

これらの到達件数、decision impact、tail tickerは未計測である。本書をcommitするまで、候補別の該当件数、現行と反実仮想のpass/rank差、採否結果を計測しない。既に確認したlatest runの総candidate件数とschema metadataは入力存在確認に限り、仮説の採否に使わない。

## 2. authorityと比較面

### 2.1 現在断面

production reader、`build_universe`、`build_metrics`、`build_candidate`、public `screening run` / `select`を使い、`2026-08-12`の全普通株を再構成する。独自SQLだけでcandidateを再実装しない。次の集合を別々に数える。

1. structural universe
2. `in_population`
3. evidence hit
4. selection liquidity通過
5. E[r]ありのranking-eligible
6. audit selection
7. application DBで現在publishされているshortlist

集合を混ぜず、各異常について母数、件数、割合、ticker、playbook、E[r]、rank、source dateを残す。

### 2.2 履歴とtail

- `jquants_fin_summaries`は対象期間の全行を走査し、欠損率、source日のずれ、同一ticker内の前年差、式の残差を分布とtailで出す。
- corporate actionは`adjustment_factor != 1`の全行と、その前後のsummaryを確認する。銘柄を先に選ばない。
- calibrationはproduction manifestに属する全panel rowを使い、current candidateだけに現れない過去regimeを確認する。将来情報を補完しない。
- p50 / p95 / p99 / maxは絶対値の昇順nearest-rankとする。最大tail上位20件と、中央値付近1件を独立式で再計算する。

### 2.3 判定権限

本監査には次の2種類のauthorityだけを与える。

- **contract correctness**: 明示config、reference、型の意味、会計恒等式、as-of契約に反する到達可能な行が1件でもあり、production関数のmutationで同じ経路を再現できれば修正してよい。件数の多さを正しさの代替にしない。
- **output-preserving refactor**: current全件でcandidate payload、E[r]、gate、rankが同値で、重複したbasis決定を1か所へ集約し、negative testで誤接続を止められる場合だけ採用してよい。

新しい投資heuristic、閾値変更、欠損補完はこの監査から採用しない。実現returnの改善を主張する変更は`estimate-calibration`の3y / 5y authorityを別途必要とする。schema追加や永続stateは、確認済み欠陥を既存fieldでは表現できず、production該当行が存在する場合に限る。

## 3. 事前登録する反証シナリオ

### S1. 発行済・自己株・平均株式のcapital basis

**破綻予測:** `shares_outstanding`へperiod-average sharesがfallbackしている、または異なる開示日のissued / treasuryを合成している場合、自己株控除後株式数が過小になり、時価総額、EPS、PBR、FCF yield、net share changeが同じ向きへ歪む。実データでは`shares_outstanding == average_shares`かつ`treasury_shares > 0`、issued source dateとtreasury source dateの不一致、`issued <= treasury`、開示前後の不連続として現れる。sourceの`ShOutFY` coverageが高ければperiod-average fallbackのproduction影響は0件と予測するが、field別carry-forwardのsource日ずれは非0件と予測する。

**固定計測:** 全summaryとcurrent全tickerについてgross issued、treasury、ex-treasury、average shares、各source date、split-normalized値を出す。`market_cap = close * shares_ex_treasury`、`EPS = profit / shares_ex_treasury`とsource EPSの役割差、`BPS * shares_ex_treasury`とequityの残差を別々に測る。period-averageを期末issuedの代替に使った行は、代替なし反実仮想とのmarket cap / multiple / gate / rank差を出す。

**停止・採否:** 異なるcapital basisを同じfieldへ格納した到達可能な行、または`issued <= treasury`を有効値として通す行はcorrectness defectとする。source日が異なるだけでは欠陥とせず、同一capital snapshotとして合成した結果が一次sourceまたは同日恒等式と矛盾し、candidate判断が変わる場合だけ修正する。

### S2. split、併合、buybackを跨ぐ株式基準

**破綻予測:** factorの向き、適用区間、interim record-dateのどれかが誤ると、action前後でper-share値だけがfactor倍、またはmarket capだけが逆factor倍になる。実データでは`adjustment_factor != 1`の前後、share前年差のp99 tail、`split_adjustment_flag`銘柄、buyback比率tailに集中する。現行v4修正後は単純splitでの残差0件、interim近傍に限定されたwarningだけが残ると予測する。

**固定計測:** 全actionについて前後bar、開示日、期末、配当基準日、issued / treasury / average sharesを同じas-of basisへ変換し、market cap、EPS、BPS、DPS、net share changeの連続性を検査する。factorを逆転、境界日を1日ずらす、interim flagを外すmutationがfixtureで必ず失敗することを求める。

**停止・採否:** 一次sourceでactionを確認できないtailは`未検証`とし、推測で補正しない。確認済みactionでfactorの向き・区間が違う行は件数にかかわらず修正する。warningの追加だけで数値誤りを隠さない。

### S3. 期間・entity・会計量のbasis

**破綻予測:** quarterly / FY、consolidated / non-consolidated、TTM / point-in-timeを混ぜると、fiscal-year change、訂正、赤字転換、M&Aの周辺で残差tailになる。実データでは`profit_ttm / denominator`とreported EPS、`equity / total_assets`とreported ratio、営業CF−投資CFとFCF、sales / operating-profit YoYのperiod pair不一致へ現れる。通常行の大半は丸め差内と予測する。

**固定計測:** 利用可能な全同一行identityとcurrent candidateのderived metricを独立再計算し、relative residualをp50 / p95 / p99 / maxで出す。FY期間長、accounting standard、consolidation、訂正順序ごとに層別し、異なるbasis同士を無理に一致させない。

**停止・採否:** 同じquantity / period / entityと実装が宣言する組だけをcorrectness判定に使う。残差tailの一次行を確認し、別basisなら仮説を棄却、同basisで式が違えば修正する。

### S4. populationと流動性境界

**破綻予測:** predicate前の丸めにより、raw market capが100億円未満でも100億円、20日平均売買代金が1.0億円未満でも1.0億円となって通る境界行が存在する。件数は全体のごく一部で、audit top rankへの影響は小さいと予測する。sector medianのfallback自体はprovenance付きで再構成可能と予測する。

**固定計測:** 全current universeでraw値と保存値を比較し、各閾値の直下・一致・直上、null、ちょうど20本、listing-span境界を列挙する。rounded predicateとraw predicateの集合差、そのtickerのevidence / E[r] / rank、sector medianのgroup n / fallback sourceを出す。

**停止・採否:** 現行referenceが丸め済みfactを閾値basisと明示している限り、集合差だけで変更しない。表示上の丸めではなく経済的なraw floorをcontractとして要求している証拠、または別surfaceがraw basisと主張しながらrounded値を使う証拠がある場合だけcorrectness defectとする。

### S5. as-of、訂正、future leakage

**破綻予測:** 正常実行では`disclosed_at > asof`、`traded_at > asof`、将来masterがcurrent metricへ入る行は0件と予測する。壊れるなら、latest取得後にas-of filterするreader、同日訂正の上書き、FY / interimのsort tie、上場・上場廃止境界に現れる。

**固定計測:** current candidateが参照する全source dateの最大値をfield別に出す。未来bar、未来summary、同日訂正、順序を入れ替えたsummaryを一つずつ注入し、as-of以前のoutputが不変か、曖昧な訂正がfail closedかをproduction関数で検査する。storeが失った同日履歴は復元したと推測せず、観測不能として記録する。

**停止・採否:** future rowでas-of outputが変われば件数にかかわらず修正する。履歴を保存していないため証明不能な訂正問題は、新schemaを憶測で追加せず`insufficient`とする。

### S6. FV / E[r]の合成と欠損から0への写像

**破綻予測:** sector / self-rangeのFV basisまたはannualizationがずれると、保存E[r]を構成項から復元できない。正常系ではreversion、dividend yield、net share changeから全件再構成できると予測する。dividend / net-share-change欠損の0写像は非0件だが、現行modelの明示contractであり、今回のcorrectness変更にはならないと予測する。

**固定計測:** 全current candidateと全calibration panel rowでFV anchor、clipped reversion、carry各項、E[r]を別計算し、保存値との差を出す。nullを0、row除外、unknownの3反実仮想に分け、ranking-eligible件数、top-20 membership、rank deltaを示す。sector fallbackとself-range欠損を分離する。

**停止・採否:** 保存式と独立再計算の差が絶対`1e-12`を超えれば調査し、serialization以外の式差なら修正する。欠損の扱いを変更するのはcorrectnessではなくmodel変更なので、本監査では結果が大きくても採用しない。

### S7. playbook gateのfail-closed性と観測可能性

**破綻予測:** `fcf_yield_required_positive`が真でも`fcf_yield is None`の行が`cashflow-yield-discount`を通るproduction hitは非0件と予測する。この場合は明示ruleに反する。`operating_profit_yoy is None`のままcash-rich / cashflow playbookを通り、共通`deterioration_gate_unmeasurable`が偽の行も非0件と予測するが、現行gateが「利用可能な場合だけ」と宣言しているなら数値gate変更ではなく観測性の問題として判定する。

**固定計測:** 全candidate×全playbookについて各predicateをtrue / false / unknownへ展開し、hit、near miss、unknown-pass、unknown-rejectを数える。FCF null / zero / negative、各YoYを一つだけnull、全YoY null、閾値の直下・一致・直上をmutation fixtureにする。config booleanを反転したとき対象predicateだけが変わることを求める。

**停止・採否:** required fieldのnullがpassし、同じconfigをfalseにした場合と区別できなければ到達件数1件で修正する。optional fieldはnullだけでrejectへ変えない。annotationが人間に「測定済み」と誤読させ、selection出力に該当行がある場合は、既存fieldの意味を保てる最小のtyped provenanceを検討する。

### S8. schema field、config、read/write branchの到達性

**破綻予測:** 既知の`is_common_stock`以外に、全行定数、常時null、書くが読まない、設定してもpredicateが変わらないfieldが少数残る可能性がある。特に安全を表すbooleanが常に同じなら、validatorが通っても防御は存在しない。

**固定計測:** candidate schema、selection payload、rule configの全fieldについてcurrent cardinality / null率、writer、reader、test到達を対応表にする。値を反転・欠損させるmutationで、意図したconsumerが変化するか確認する。監査用annotationはrankingを変えないことも逆向きに固定する。

**停止・採否:** 定数は即削除しない。現在のsource契約による定数か、将来用の未使用拡張か、安全branchの欠落かを区別する。安全branchが到達不能なら修正し、単なる表示冗長性はoutput互換を壊してまで整理しない。

### S9. end-to-endの保存artifactとpublish境界

**破綻予測:** 同一input・rules・codeなら`screening run`から`select`を再実行したticker、E[r]、rankは保存artifactと一致する。application DBのpublished shortlistは別時点の判断成果なので、as-of / run identityを揃えない比較では差があり得る。

**固定計測:** pre-fix baselineとpost-fixを同じstore snapshotでrun / selectし、candidate全field、evidence、E[r]、rank、top-20を比較する。published shortlistは紐づくrun / selection identityを先に解決し、同一identityならexact一致、異なるidentityならticker集合差と理由を記録する。新しいrunをpublishせず、application DBを書き換えない。

**停止・採否:** 説明できない非決定性、同一identityのticker / E[r] / rank不一致は修正対象とする。時点違いをロジック回帰と数えない。

## 4. 固定する出力

結果reportと機械JSONへ最低限次を残す。

- repository commit、rules SHA-256、各SQLite identity / schema / row count / date range、実行command、artifact SHA-256
- S1〜S9ごとの母数、件数、割合、p50 / p95 / p99 / max、tail上位20、decision impact
- currentのuniverse → population → evidence → liquidity → E[r] → selectionのfunnel
- pre-fix / counterfactual / post-fixのcandidate、playbook hit、E[r]、rank、top-20集合差
- 各仮説の`confirmed` / `rejected` / `insufficient`、根拠、production変更の有無
- 一次source確認を要したticker、確認URL / source row、未検証項目
- mutation一覧、変更前に失敗すること、変更後に通ること、隣接経路が不変であること
- schema / calibration / cloud反映の要否と、その根拠

JSON集計はproduction関数のoutputから作り、独立検算は別のSQLまたは最小式で行う。同じhelperを左右両方のoracleにしない。

## 5. 総合採否と変更境界

1. `confirmed`は、実データの到達行、明示contractとの不一致、独立再計算、失敗するmutationの4点が揃うことを原則とする。fixtureだけ、件数だけ、commentだけで断定しない。
2. correctness defectは最小surfaceで修正し、変更前失敗・変更後成功のregression testを置く。隣接するoptional欠損や別playbookを同時に厳しくしない。
3. `rejected`は成功結果と同じ粒度で母数、tail、反証根拠を記録する。0件を省略しない。
4. `insufficient`はsourceを推定せず、production変更をしない。外部一次sourceが403なら別の一次sourceで突合し、確認できなければ未検証のままにする。
5. refactorはpre/post全current output同値とmutation感度の改善を必須とする。将来利用を仮定したfield、CLI、schema、stateを増やさない。
6. calibration code / E[r] / FV / rankを変えた場合だけ全production calibrationを再構築し、`production_change_allowed: true`とcontext hash一致を要求する。変えない場合も既存drift gateを通す。
7. store schemaを上げた場合だけ、merge後にローカル移行、`integrity_check`、移行前後行数照合、cloud copy包含、同一作業内pushを行う。schema変更がなければcloud storeを更新しない。

## 6. 解釈の限界

この監査は現在のsource coverageと既存長期panelで、式と判断面の整合を検査する。未観測の会計訂正、providerが保存しなかった版、将来のsource語彙までは証明しない。current cross-sectionで順位差が大きくても将来returnの改善とは読まず、差が0件でもmutationで到達可能なfail-openを安全とみなさない。財務的にもっともらしい値であることと、source・期間・entity・株式基準が正しいことを分けて判定する。
