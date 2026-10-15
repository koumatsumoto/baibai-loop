---
title: "見積り較正"
summary: "point-in-time panelと長期forward returnでE[r]・FV・候補一覧方法を較正する契約。"
doc_type: reference
status: active
---

<a id="estimate-calibration"></a>

# estimate-calibration — 見積り較正

本書は、screeningの機械見積りと選定順位を過去as-ofで再構成し、実現した価格リターンと突き合わせるローカル専用の較正契約を所有する。portfolio outcomeやJPX total-return benchmarkとは別の横断的な見積り診断である。

評価の実装は[evaluation](../../engine/src/baibai_engine/screening/calibration/evaluation/)に集約する。`cohorts`が評価を組み立て、`axes`は軸とquality、`discovery`は候補再現、`sensitivity`は欠落によるbias、`aggregate`はcohort横断の集計を所有する。採用済みの評価条件は`policy`、共通統計は`statistics`を参照する。

<a id="horizon-evidence-role"></a>

## Horizonごとのevidence role

| horizon | targetの決め方 | evidence role |
| --- | --- | --- |
| `3m` / `6m` | 暦月を加算 | regression alert |
| `1y` | 暦年を加算 | leading evidence |
| `3y` / `5y` | 暦年を加算 | empirical change evidence |

`calibration-evaluate`の通常実行は全horizonをdiagnosticとして出力し、`evidence_readiness`を持たない。実証的なscreen、ranking、E[r] policy parameterの変更を人間が検討するときは、`--run-purpose empirical_change_evidence`でrequired as-ofとdecision subjectを明示する。3y / 5yのinput、outcome、method、必須metricが揃えば`evidence_complete: true`になるが、効果の符号や採否は表さない。artifactは設定やコードを自動変更しない。

Candidate Discoveryのevidence subjectは`candidate_discovery_approach`または`candidate_discovery_nomination_union`とする。approach subjectは対象approachの設定済みnomination_depth全件のoutcome（`approach_*_nominations`）、候補一覧 union全体、対象approach fidelity、union fidelityを必須にする。union subjectは候補一覧 union全体、union fidelity、全4 Approach fidelityを必須にする。固定top5/10/20のoutcomeは設定された全候補のoutcomeと区別する。pure E[r] top20はsecondary priorとの比較diagnosticであり、Candidate Discoveryの必須metricにしない。必須metricはsubjectから導出し、`--required-metric`は事前登録固有の追加guardrailだけに使う。既定の`estimator_policy`は`er_calibration`だけを必須にし、Candidate Discovery method fidelityを代替しない。

target は cohort の actual as-of date に calendar month を加算する。元の日が calendar month-end の場合は対象月末を保ち、非取引日は target 以下の最終取引日に解決する。

<a id="data-integrity"></a>

## データ完全性

panel は cohort as-of 以下の最新 `eq_master` snapshot だけを読む。prior snapshot、snapshot unavailable、survivorship、delisting、corporate-action event coverage の不備は payload に残り、3y/5y evidence を block する。

`evidence_readiness`は、現在のsnapshotが保持するpanel / forward / diagnosticsの
integrity、3y/5y coverage、subject必須metric、追加guardrail、measurement policyだけで決める。過去入力の
snapshot identity や bytes は保持せず、将来の code で完全再実行できることも別の
保証として要求しない。

forward row は解決済み status（市場終値による `resolved`、成立した現金公開買付けによる `resolved_control_event_exit`、破綻型の上場廃止による `resolved_failure_exit`）または明示的な unresolved status を持ち、`resolved` flag は前者 3 つと一致する。target と entry はそれぞれ target/as-of 以下の最終取引日で解決し、15 日超の stale exit は resolved return に入れない。価格は as-of basis adjustment factor で正規化するが、metric basis は `price_return_only` であり配当 accrual を加えない。entry 時点の配当利回りを horizon 年数で按分する固定 accrual は、期間中の増配・減配・無配・支払時期を観測した実現配当ではないため、実現値として扱わない。

財務サマリーの購読窓は 10 年の移動窓であり、store が読み取りを許す最古の日付は日々進む。panel の履歴窓（正規化 EPS 2,200 日、株主還元 1,200 日）はこの下限で切られるので、下限に近い古い cohort ほど履歴が短く、必要な期数に届かない値は null で出る。窓が通り過ぎた行は table に残るが読まない。**下限は store が持つ最古の行ではなく coverage が答える範囲から取る。** 両者は同じ「履歴の始まり」を指しながら別の量であり、行の側を採ると source が出せない範囲を要求して全 cohort が構築不能になる。

<a id="coverage-verdicts"></a>

### Coverage 判定の導出

3 つの coverage は cache された観測から評価時に導出する。cohort が書かれた時点の契約ではなく現行契約で判定するためであり、既存 cohort も再構築せずに判定し直せる。

3y/5y の blocker は 1 観測につき 1 つだけ立てる。ある判定から導ける別の判定を並べると、同じ欠けを二重に数えて理由の内訳が読めなくなるためである。

評価では、as-ofの母集団、価格調整factor、入力窓とpanel/forwardの集合、未解決returnの分類、除外が結論に与える影響を別々に確認する。masterが当日断面でない、必要入力がclampされる、集合が一致しない等の不備は、単なる件数不足にまとめない。厳密な判定と出力fieldは[evidence](../../engine/src/baibai_engine/screening/calibration/evidence.py)と[evaluation](../../engine/src/baibai_engine/screening/calibration/evaluation/)が所有する。以下では結果の解釈に必要な限界を扱う。

survivorship は population の性質なので panel が件数を測り、verdict は読み手が件数から導く（凍結すると complete の定義を変えたときに既存 cohort へ届かない）。bar store は市場から消えた銘柄の価格も保持するため、**as-of 当日に価格が付いた集合**を master snapshot と独立に観測できる。master が as-of より後なら当時上場していて現在は廃止された銘柄を欠き、前なら以降に上場した銘柄を欠く。どちらも断面が as-of の投資可能 universe ではないので incomplete とする。当日を基準にするのは、as-of 前に最終売買を終えた銘柄を master が持たないのは正しいからで、entry の staleness 許容（15 日）をここへ流用するとどの master でも mismatch を 0 にできなくなる。計測前に書かれた panel は件数を null として報告する（未計測を「欠けなし」と読めないようにする）。

EDINET の取込は最近の as-of 分しか無いので、それ以前の cohort は `edinet_axis_population_count` が 0 になり得る。0 の cohort は EDINET 由来の軸を 1 つも持たずに screen を再現しており、**production が実際に走らせている screen とは入力の違う screen である。** `edinet_axis_population_count`はその差を読むdiagnosticであり、単独でblockerにはしない。件数はEDINET由来の軸をいずれか持つ銘柄を数えるため、各銘柄が持つ軸数は区別しない。`null` は未計測、0 は計測して該当なしであり、同じではない。

`adjustment_factor_coverage` は bar store が答えられる唯一の corporate-action 観測である。系列を調整しない action（合併の対価、株主割当増資）はローカルに source が無いので、この残余は判定に畳まず、外部 source を要する既知の限界として扱う。

`unpriced_exit` / `adjustment_factor` の verdict は、survivorship が complete な断面でのみ意味を持つ。population から既に落ちている銘柄については系列終了も factor 欠落も観測され得ないので、survivorship が incomplete な cohort でこの 2 つが `complete` に見えるのは「濾された後の集合が綺麗」という意味にすぎない。

### 未解決 row の分類

未解決 row は 1 種類の欠陥ではないので、evidence readiness は総数ではなく分類ごとの件数を読む。

| 分類 | 意味 | gate への影響 |
| --- | --- | --- |
| `entry_unpriced_at_asof_count` | panel も as-of 基準の価格を持たない。未上場の確認ではない | block しない。production screen も同じ銘柄を universe から落とすので、較正の母集団は screen が選び得た集合と一致する |
| `entry_price_gap_count` | panel は as-of の価格を持つのに forward が entry を持たない | block する。断面に数えた銘柄の forward 観測が無いので、population を無言で欠く |
| `unpriced_exit_count` | 窓中に系列が終わる（廃止 exit value なし） | 件数では block しない。`delisting_exclusion` が結論の頑健性で判定する（下記） |
| `future_horizon_count` | target が評価可能な最終取引日より先 | block する。cohort が満期に達していない |
| `unclassified_unresolved_count` | 上のどれにも入らない未解決 status | block する。分類は allowlist なので、status が増えた日に無音で通らないための残余 |

`entry_not_listed` が非 block なのは「市場に無かった」に限らないので、panel が price を持ちながら universe へ入れられなかった銘柄は row の `population_coverage_status` と `priced_master_without_universe_count` で別に同定する。universe の除外条件が増えても、その分が非 block の側へ黙って流れ込まない。

entry は as-of の 15 日前までの close で解決するので、保有期間は名目 horizon より最大でその分長い。価格 bar と `adjustment_factor_coverage` はこの窓から読む。一方、実現配当を支払ごとに株式基準へ換算する corporate-action event は、最初に対象になる FY の `period_start` まで別に遡って読む。価格入力窓と配当の会計期間窓は同一ではない。

### universe 未評価銘柄（`priced_master_without_universe`）

該当 row は必要な入力履歴を欠くため valuation metrics、rank、E[r] を持たず、窓中に価格系列が終われば実現 forward return も持たない。現行 method で選抜対象にならない row へ所属を後付けせず、観測済み return の有無が母集団中央値を通じて production 結論の向きを作っていないかを有界バイアスで判定する。

報告値では観測済み return だけを使い、未解決 return は値なしのまま母集団から除外する。感度計算では resolved / unresolved を問わず該当 row だけを `-1.0` と置換前の resolved common eligible母集団中央値へそれぞれ置換する。Nomination union全体と`er_calibration`の向きは delisting 判定と同じ定義を使い、報告値と両置換の向きがすべて一致するときだけ `direction_stable` とする。`resolved_target_count` と `resolution_complete` は観測できた実現 return の coverage 診断であり、単独ではevidence completenessをblockしない。

cache が対象 row を同定できない、diagnostics 件数と row 数が一致しない、または両側代入で向きが割れる場合は fail closed で block する。この判定は欠けた実現 return、metrics、rank を復元せず、未評価銘柄が無かったことにもならない。

### 廃止銘柄の除外（`delisting_exclusion`）

窓中に系列が終わる銘柄のうち、成立した現金公開買付けの価格を確認できたものは、その価格を退出価値の代理値として使う（後述「支配権イベントの exit 代理値」）。対象理由による最終市場終値の規約も後述する。どちらの退出値も確定しない銘柄は unresolved に残す。未解決件数だけから較正不能と決めず、除外対象への固定ケース代入で結論方向が変わるかを cohort ごとに判定する。すべての cohort が未解決銘柄を含むとは仮定しない。

除外された銘柄へ全損・中立の二つのケースを代入して結論を再計算し、**cohort が報告した値と両方の代入とで向きが一致するときだけ** `direction_stable` を立てる。

| 代入 | 値 |
| --- | --- |
| 全損 | `price_return = -1.0` |
| 中立 | 同 cohort の resolved common eligible母集団の中央値。benchmarkや母集団外の銘柄は含めない |

報告値を比較に含めるのは、それが evidence readiness の読む値そのものだからである。両方の代入で向きが揃っても報告値だけが逆を向くなら、その結論は除外が作ったものになる。向きは`review_set_all`がNomination union全体の`median_excess`の符号、`er_calibration`が最上位 quintile の`median_realized_price_excess` − 最下位 quintile の同値の符号で定める。いずれかの場合で値が算出できず他の場合で算出できるときも、除外が「cohort が何か言えるかどうか」を決めているので不安定として扱う。

この判定は結論を下へ引く可能性に対しての bracket である。買収による廃止はプレミアム付きで中立代入の上に出るため、上側は挟まない。実値で解決できた行はこの bracket の対象から外れる。

## 較正座標

### 価格収束E[r]

`er_calibration` は価格収束成分 `er_reversion_annual` だけを price-only 実現値へ較正する。予測値は cohort 内の `er_reversion_annual` 中央値、実現値は同じ cohort の price return 中央値をそれぞれ引き、quintile ごとに median の相対値を比較する。`calibration_error` は `realized - predicted` である。配当と buyback の carry は price-only 実現値と同じ basis で観測できないため、この座標で絶対水準を較正しない。carry の妥当性は source と算出 contract を検証し、実現配当を備えた total-return dataset が利用できる場合に別の較正座標で扱う。

### Cohort比較のbasis

cohort比較は`--basis price|total`を取り、母集団は保存された`PanelRow.in_population`に従う。totalはFY配当観測を要するため、priceと評価可能な母数が一致するとは限らない。`basis_coverage`と`bases_comparable`を確認し、被覆不足の二つの中央値を直接比較しない。既定はpriceで、具体的な被覆率は対象scopeを伴う結果へ記録する。

### 自己株式取得枠

自己株取得を含む資本配分は、選ばれた銘柄のresearchで一次開示を読む。機械E[r]のcarryが使う`net_share_change_yoy`は過去の株数変化・希薄化signalであり、将来のbuyback cashや未消化枠とは呼ばない。

<a id="diagnostic-thresholds"></a>

### 業績変化による診断群

`gates`は業績変化の条件で群を分ける診断座標であり、現行Candidate Discovery全体の除外gateではない。診断条件はcalibration evaluator、実際の選定は各Approachが所有する。

固定-30%は群の差を測るための事前固定帯で、現在の購入資格や投資不適格の境界ではない。旧production settingの削除後も診断の意味は変えず、[ルールの目的](./screening-runtime.md#rule-purpose)に従って運用上限・経済仮定・入力妥当性を混同しない。

### 業種中央値basis

`sector_median_basis` 座標は `smg_*` 軸を、業種中央値から作られた行と市場中央値へ落ちた行に分けて測る。素性は `PanelRow.smg_market_fallback` が持つ。fallbackでは比較基準の業種構成が変わるため、分けないと同業比較と市場比較を同じ数字として読んでしまう。どの業種・倍率に偏るかは対象断面で確認する。

`group_median_excess`は群の構成も反映し、`axis_effect.median_excess_delta`は群の中を軸値で分割した差を測る。構成の違う二群の平均値だけで軸の優劣を決めない。

`mean_axis_effect`は`stdev_axis_effect`と併せて読む。月次cohortの窓は重複し、cohort数は独立標本数ではない。母数不足でspreadを出せない場合と、測定した効果がない場合を区別する。具体的な母数・効果量・shuffle結果はhistorical reportへ残す。

### E[r]絶対水準

`er_level_calibration` は E[r] 合計の絶対年率と、実績 FY 配当を加えた実現 total return の絶対年率を `er_annual` quintile ごとに比較する。実現配当は `entry_date < fiscal_year_end <= exit_date` の FY 行を対象に、同じ FY の最新 non-null `DivAnn` を forward の比較基準日（eval cap、未指定時は最終価格日）の株式単位へ正規化して合算する。同日開示は開示時刻まで比較し、時刻不明を新旧の根拠にしない。同じ最新revisionの配当・換算に使う株数等が矛盾する場合も未解決に残す。対象 FY 行なし、`DivAnn` 欠損、adjustment factor 不完全は 0 円とせず total-return 側を unresolved にする。明示された `DivAnn == 0` は観測済み無配である。端の FY は月割りしないため、この座標は実際の中間・期末配当の権利落ち日を再現する cash-flow ledger ではない。

保有期間中にライツイシュー（`ExRT = 3`）があれば、権利売却の受取額や権利行使の追加払込額を価格factorだけでは復元できないため、total return は `unresolved_rights_issue` とする。factor が1でもこの区別は変わらない。保有終了後のライツイシューで共通の比較価格基準が変わる場合は、配当成分にも同じ価格換算を掛け、終了済みのリターンを変えない。この比較基準上の配当合計は、そのまま口座の現金入金額を表すものではない。

## 判断面へ渡す較正文脈

<a id="estimator-vs-decision"></a>

機械E[r]の収束係数・clip・固定8.5%帯は近似推計と歴史分布の座標であり、企業判断の[通常購入6%](../portfolio-management.md#required-return-policy)とは別の仮定である。6%は暫定政策で、最適値や確率加重期待収益を実証した値ではない。E[r]較正の成立は4 Approachのfidelity、個別企業の利益・倍率・分配の見積り、購入採否を代行しない。係数の優劣を主張する変更は事前登録と3y/5y evidenceで検証し、診断帯を購入floorに追随させて統計を付け替えない。

<a id="er-level-distribution-context"></a>

意味契約は`valuation/calibration/contracts`、保存済み文脈の期限・method identity判定と評価帯解釈は`valuation/calibration/query`が所有する。較正実行・evidence生成・候補比較はScreeningに残す。配置と依存は[architecture](../architecture.md#repository-map)を参照する。

UIと必要なResearchが参照する最新文脈の正本は `reports/published/er-level-calibration-latest.yaml` である。`calibration-evaluate --context-out` は、明示した全required月の有限E[r]母集団を保持して、3y / 5yの観測対象の歴史分布を生成する。formal evidence completenessは別に記録し、正常なentry/exit・配当の観測不能だけで全分布を隠さない。panel欠落、入力・forward partitionの不整合、未知の未解決状態、method不一致はpartialとして扱わず拒否する。

schema 4では、結果の観測可否で予測帯を選ばない。宣言母集団の有限E[r]全対象から各cohortの五分位上限を求め、そのcohort中央値を小数6桁で固定する。同じrequired月では両horizon・両basisで境界を共有し、履歴と候補を同じ境界へ分類する。Q1は上限以下、Q2〜Q4は下限より大きく上限以下、Q5は下限より大きい。同じE[r]をtickerで分割せず、8.5%の独立帯は下限以上を集計する。丸め後の境界重複は拒否する。旧schemaの統計を新しい母数の統計へ付け替えない。

各basisは宣言数・観測数・未評価数と既存statusの理由内訳を保持する。配当欠測でも観測済みpriceを残し、totalへpriceや配当0を代用しない。ticker-as-of等重みの年率median / q25 / q10 / trap rateは観測済み対象の統計で、cohort等重みも観測のある月から計算する。宣言月数と観測月数、月次観測数の中央値を併記し、観測が全くないbasisの分布はnullにする。bandのmedian nは結果を問わない宣言予測数、basisのmedian nは宣言全月（観測0の月を含む）の観測数である。

これは未評価を含む全母集団の実証済期待収益・個別予測・購入条件ではない。未評価は損益0として集計しない。basisごとに観測対象と累積returnの比較中央値が異なり、price-total差を配当効果として解釈しない。Research入力と既存Stock Analysis表示は分母・不足理由・formal completenessとこの限界を一緒に渡す。購入条件やselection/orderは変更しない。

UIとResearchの明示照会は、artifact の `screening_rules_hash` と `er_model_version` が実際に表示・調査する operative run / 候補一覧 の identity と一致するときだけ、候補 E[r] を該当 quintileへ対応づける。identity不明・不一致は較正文脈を利用不能として扱い、Research全体の新hard gateにしない。E[r]、順位、選定、FVは変更しない。値は個別銘柄の予測ではなく historical distribution であり、重複する月次窓を独立標本と呼ばない。

配当予想の鮮度など計算入力の意味を変える場合は、既存の`VALUATION_CALCULATION_REVISION`を更新する。productionのrules hash、panelのrules hash、派生cache contractへ同じrevisionを伝え、旧panelの再利用と旧contextの新runへの適用を拒否する。ER算式が同じなら`expected-return-v1`は維持する。保持artifactの旧計算revision・scope・入力窓はprovenance検証だけで認識し、元bytes/hashを変更しない。複数の変更を統合する場合、panel・contextの再生成は承認済みの最終methodでまとめて行う。

rules変更後に対応する長期較正文脈がまだない場合、旧artifactは元のhash・統計・生成日で保持する。現行schemaのrules不一致は`rules_identity_mismatch`、旧schemaはidentityが一致しても`invalid_artifact`として非表示にする。CIは保持する旧schema 2の原bytesと凍結rulesへのprovenanceを検証し、runtimeへ互換読取を加えない。現行schemaの構造とrunへの適用可能性はreaderが検証する。最新schema4への更新結果と元bytesの旧artifactは[v26固定19月の受入記録](../../reports/studies/2026-10-14-er-context-v26/report.md)を参照する。旧統計のhash/schemaだけを新定義へ差し替えず、保存済みの整合したpanel/forwardと明示required scopeから`calibration-evaluate --context-out`で再生成する。規模・bar期間・FCFの条件を変えれば比較母集団も変わるため、旧較正を新methodの証拠としない。

`er_gte_8_5pct`は機械E[r]が8.5%以上の固定較正帯で、thresholdは`HURDLE_ER_ANNUAL=0.085`。これは[通常購入の要求年率](../portfolio-management.md#required-return-policy)へ連動する帯ではなく、policy変更時もband identityと過去分布を維持する。

artifact は生成日から45日だけ有効とし、月次の calibration 更新後に同じ production scope の評価から再生成する。欠損、schema / basis / quintile 境界不正、現在 method または operative run との identity 不一致、run identity 不明、未来日、45日を超える期限、期限切れでは read model が文脈全体を非表示にする。YAML を手編集して更新しない。

`calibration-evaluate` の artifact は、operative runと照合する`screening_rules_hash`と、panelのCandidate Discovery replay contractまで含む`calibration_method_hash`を分ける。各 `(asof, horizon)` の `integrity_status`、required metric別 status、blocking reasonを `cohort_integrity` に持つ。Candidate Discovery については、approachごとの nomination / resolved nomination、同日JPX規制入力の状態、候補一覧とexact Nomination unionの一致を `candidate_discovery_fidelity` に持つ。各 horizon は満期済みcohort数だけでなく、entry year別件数と重複しないforward window数を `observation_dependence` に開示する。月次cohort数を独立標本数とは読まない。study はこの評価結果を読み、独自の辞書リテラルで eligibility を作らない。

forward row は price-only の `price_return` / `status` と、`realized_dividend_sum` / `realized_dividend_fy_count` / `total_return` / `total_return_status` / `total_return_basis` を別々に持つ。`total_return_status == resolved` の row だけが level metric に入り、既存 price-only metric の母集団と値は変えない。component 表の realized dividend は annualized(total) − annualized(price) で、予測 carry に含まれる buyback を直接観測しない。

## Methodとcacheの互換性

`er_level_calibration`、`margin_short_to_adv`、`normalized_per_3fy`はsubject必須metricではなくoptionalな既知metricである。事前登録固有の追加guardrailにする場合だけ`--required-metric`へ明示する。

`normalized_per_3fy`がcalibrationのsubject mandatory metricではないことは、Candidate Discoveryで未使用という意味ではない。Normalized Earnings Powerのnative eligibility/orderには使い、FV/E[r] estimator入力には使わない。

候補正本の統合後も、`PanelRow.in_review_set`、`valuation_approaches`、`valuation_approach_ranks`、PanelDiagnosticsの`security_analyses`/`nominated_candidates`、evidenceの`candidate_discovery_nomination_union`等は測定wireとして保持する。これらは独立publicationではなく候補集合membershipの測定値である。productionと較正は同じ丸め済みStock Analysisへ`build_candidate_contexts`を一度適用し、理由から測定値を導出する。cohort/forward/ER artifactをrun cacheの切替で消したり、現在identityへ書換えたりしない。

cache schema versionは[store実装](../../engine/src/baibai_engine/screening/calibration/store.py)がfieldと計算revision等から導出する。計算の意味変更は所有領域のrevision（`VALUATION_CALCULATION_REVISION` / `MARKET_CONTEXT_CALCULATION_REVISION`）を明示更新する。run・panel・cacheは両方をidentityに含め、旧計算の再利用を防ぐ。保存済み資料のidentityを新値へ付け替えない。市場storeの整数`user_version`とは別契約である。panelは、productionの730日財務入力を変えずに補助履歴から、3 FYのsplit-safe DPS、DPS YoY・予想増配・配当開始、グロス株数減少streakと還元変化composite、赤字を含む連続3/5 FYのsplit-safe平均EPSによる正規化PER、PIT-TTMの`operating_profit_to_assets`・`operating_margin`・`asset_turnover`を記録する。これらのpanel列と`ProfitabilityLevelSignals`はcalibration専用で、productionへ渡さない。production ReinvestmentはStock Analysisから別途収益性を計算し、[native eligibility/order](./screening-runtime.md#candidate-discovery)に使う。グロス株数減少は自己株取得の事実ではなく、消却・発行等の純変化proxyである。`rules_hash`はrules・variant・入力窓・Valuation / Market Context calculation revision・Candidate Discovery method hashを含む。valuationの式・資本分母・価格基準またはCandidate Discoveryのeligibility・order・Nomination union contractが異なるpanelは、method identityとcache schemaの不一致でfail closedにする。

報告空売り残高の L1 は disclosure date と calculation date を分け、reporter 名tupleと住所、ratio / shares / units、前回報告値、Notes、provider row ordinalを保存する。Notes は自由記述であり、取消フラグへ推測変換しない。panel の `reported_short_ratio` / `reported_short_breadth` / `reported_short_latest_disclosed_at` は両日が cohort as-of 以下の最新stateだけを集約する。開示日は有効な報告の実際の日付であり、観測済み無報告のratio/breadthが0でも開示日はnullのままにする。coverageの基準日を開示日として補わない。公式 dataset floor から連続coverageを証明できる場合だけ無報告を明示的0とし、plan floor、coverage gap、同率最新stateの競合、最新ratioの欠測・負値では該当値をnullにする。0は「0.5%未満または報告不在」であって空売り不存在を意味しない。[JPX の訂正例](https://www.jpx.co.jp/markets/public/short-selling/tvdivq0000002qzg-att/example_2.pdf)の明示された数値ゼロと、数値を欠く報告は区別する。この軸も calibration annotation 専用である。

信用需給では、公表済みの直近残高（2026-09-18 まで全銘柄週次、以後は全銘柄日次）を source として、貸借銘柄だけの `margin_short_to_adv` と、交絡確認用の60取引日 realized volatilityを保持する。列の語義は cadence で変わらない（[`margin-publication-transition.md`](./margin-publication-transition.md) §6）。`margin_std_long_share` は判断面へ出す文脈 annotation であり、candidates・rank・候補一覧を変えない。空売り残/ADVを追加guardrailにする事前登録済みrunだけが`--required-metric margin_short_to_adv`を指定する。missing/mismatch/partial cache は `calibration-build --force` で再構築する。保存形式は[`market-lake.md`](./market-lake.md#較正store)を正本とする。

`rules_hash` は `ScreeningRules` の JSON dump 全体から作る。したがって **panel の値を 1 つも変えられない変更（無効な knob の削除・field の並べ替え）でも hash は動き、store 全体が再構築対象になる**。rules model の形を変えるときは、その再構築コストを変更の便益と比べる。

productionとproduction-authority calibrationは同じADV-freeな`candidate_discovery.common_eligibility`を使う。`avg_turnover_oku`はpanelと判断面のcontextとして保持するが、値・欠損のどちらもeligible population、Approach rank、Nomination membershipを変えない。旧ADV floorとのdual populationや比較用fallbackは持たない。

`calibration-build` と `calibration-evaluate` は同じ `--rules-path`（既定は `SCREENING_RULES_PATH` または current rules）を読む。evaluate は snapshot の `panel_variant` を含めて同じ方法で `rules_hash` を計算し、保存値との完全一致を metrics 計算前に要求する。Candidate Discovery の nomination depth は、その一致を確認した `CandidateDiscoveryRules` だけを正本とし、evaluator に別値を持たない。panel buildはcohort当日のJPX規制snapshotだけを読み、required sourceが全部揃えば`complete`、一部なら`incomplete`、snapshotが無ければ`unavailable`として保存する。規制対象tickerが0件でもrequired sourceが揃っていれば`complete`であり、未観測を空集合へ補完しない。不一致または旧cache contractではruntime migrationせず、`calibration-build --force`でcurrent methodから再構築する。

<a id="pre-2019-診断-panel"></a>

## pre-2019診断panel

`--panel-variant pre2019_self_range_375` は self-range を 375 sessions、bar 入力を 600 暦日に固定する診断専用 contract である。通常 store と異なる `--calibration-dir` が必須で、variant と窓は `rules_hash` に含まれ、全 row が `self_range_degraded: true` を持つ。この store を `--run-purpose empirical_change_evidence` で評価すると拒否する。production panel の既定窓、screening rules、evidence条件は変わらない。

この variant でも各rowの`self_range_observed_sessions`が、その contract の上限へ実際に届いたかを示す。短い履歴をfull-windowとして扱わないための列であり、production self-rangeは750 sessionsのままである。

```bash
(
  set -e
  uv run baibai-engine screening calibration-build \
    --start 2018-03-01 --end 2019-10-31 \
    --calibration-dir stores/screening/calibration/variants/pre2019 \
    --panel-variant pre2019_self_range_375 --force
  uv run baibai-engine screening calibration-evaluate \
    --calibration-dir stores/screening/calibration/variants/pre2019 \
    --horizon 1y --horizon 3y --out /tmp/calibration-pre2019.yaml
)
```

## store の再構築

対象はcalibrationのmachine snapshotであり、market L1やapplication DBではない。全再構築では保持する全cohortを覆うstart/endを指定する。先に現行snapshotを削除せず、builderのtemporary build・検証・atomic replaceを使う。

```bash
uv run baibai-engine screening calibration-build \
  --start <YYYY-MM-DD> --end <YYYY-MM-DD> --force
```

`--force`では指定範囲だけから新snapshotを作る。入力coverage不足は再構築だけでは解消しない。説明文・commentだけの変更では、method revisionを進めず再生成しない。forwardの計算規約は`FORWARD_CALCULATION_REVISION`でcache identityに含める。開示時刻・同時開示の矛盾判定へ切り替える前のsnapshotを新規約の結果として再利用せず、必要なscopeを再構築する。公開済みのhistorical artifactは改訂前の測定記録として保ち、自動で書き換えない。

<a id="commands"></a>

## コマンド

履歴入力が不足する場合だけ、必要な期間でbackfill/buildする。日付は対象scopeへ置き換える。

```bash
(
  set -e
  uv run baibai-engine screening backfill-master --month-end-from 2022-09-01 --month-end-to 2026-06-30
  uv run baibai-engine screening calibration-build --start 2019-11-01 --end 2026-07-31 --force
)
```

評価は目的に合う1つだけを選ぶ。通常diagnostic:

```bash
uv run baibai-engine screening calibration-evaluate --out .cache/calibration-eval.yaml
```

estimator policyの実証:

```bash
uv run baibai-engine screening calibration-evaluate \
  --run-purpose empirical_change_evidence --required-asof 2021-06-30
```

単一Approachのeligibility/orderの実証:

```bash
uv run baibai-engine screening calibration-evaluate \
  --run-purpose empirical_change_evidence \
  --decision-subject candidate_discovery_approach \
  --candidate-discovery-approach asset-value --required-asof 2021-06-30
```

Nomination unionの実証:

```bash
uv run baibai-engine screening calibration-evaluate \
  --run-purpose empirical_change_evidence \
  --decision-subject candidate_discovery_nomination_union --required-asof 2021-06-30
```

E[r]水準parameterを判断する事前登録済みrunだけ`--required-metric er_level_calibration`を加える。判断面の月次文脈も更新する場合は、同じ評価へ`--context-out reports/published/er-level-calibration-latest.yaml`を加える。required panelや観測構造が不整合ならcontextを書かずexit 1となる。正常な観測不能では記述contextを書き、formal evidenceの不成立と未評価を残す。context書込成功を方法変更の証拠成立へ読み替えない。

## 改善サイクルの運用契約

改善の採否は[開発原則](../doctrine.md#development-investment-policy)、作業・提出は[AGENTS](../../AGENTS.md)に従う。本節の事前登録・長期比較は経済仮説の優劣を主張する変更に適用する。計算不具合は算術・期間・単位・株式basisの回帰、運用不具合は成功・失敗経路で検証し、文書整理にStudyや本番再生成を要求しない。

### 改善対象マップ（レバーの所在）

| レバー | 所在 | 計測経路 |
| --- | --- | --- |
| Candidate Discoveryのeligibility・order・depth | `method/screening/rules/`、`screening/rule_config.py`、`screening/discovery/` | Approachとunionの較正リプレイ |
| Nomination union | 同上 + `engine/src/baibai_engine/screening/discovery/` | 較正リプレイ（Approach別Nomination + union） |
| 機械 E[r]・FV アンカー | `engine/src/baibai_engine/valuation/estimates.py` | 較正リプレイ（er 軸 IC / decile / 予測 vs 実現） |
| valuation 指標の算出 | metrics 系 + [`valuation-metrics.md`](./valuation-metrics.md) | 算術・期間・単位・株式basisの回帰。methodの優劣を主張する変更は較正リプレイ |
| マクロ判断の手順・レンズ | [Market Context](./market-context.md)・skill `market-context-assessment` | 保存した判断と後続の観測の照合 |
| research の見積り手順 | [`stock-assessment.md`](./stock-assessment.md) + skill `research` | portfolio outcome と長期 horizon calibration |
| 資本・cap・sizing | [`portfolio-management.md`](../portfolio-management.md) + `position/policy.py` | 保有 outcome |
| Researchの選定 | `research/selection_policy.py` | 保存分析・候補から深掘り対象までの実運用の検証 |

Valuation Approachを追加・変更・削除するときは、screening rules・対応Research Playbook checklist・候補一覧の構成・testを同じ変更で整合させ、根拠を較正結果に置く。

### 事前登録と design/confirm

- 仮説と採否基準は計測前にIssueまたはreportへ記す。reportはpathとcommit、IssueはURLと比較開始前に記した条件で識別する。既知の結果へ接触済みなら盲検性の限界を明記する。
- cohort を時間で design / confirm に 2 分割し、**両方で同方向・基準充足のときだけ採用**。片側のみは不確定、両側逆は棄却。grid search（基準を後から動かす網羅探索）をしない。

matched 比較の被覆率・membership 数・集中度など、forward outcome を読まずに計算できる sufficiency は、効果条件を凍結する前に実測する。不足する場合は比較設計を修正し、同じ outcome-free 指標を再測定して、あらかじめ定めた sufficiency floor をすべて満たすまで凍結しない。最終設計の実測値と変更点は同じ事前登録先へ記録する。この修正 loop は forward outcome を一度でも読んだ後には再開せず、凍結後は outcome を見て条件を調整しない。逐次 study は先行 study の効果結果で後続条件を調整せず、match 被覆不足など outcome-free な実行可能性の欠陥は手法上の教訓として後続設計へ適用できる。

- **control cell の判定は「0 許容の全 cell 通過」を既定にしない**（偽陰性へ構造的に偏る）。noise floor（例: trap delta ≤ +2pt）または k-of-n cell 通過と、cell ごとの最小 matched weight を**事前登録で宣言**する。
- 判定語彙は `negative` / `insufficient` / `adoption_candidate` / `inconclusive` の 4 種。同一仮説の再検定は新 evidence（新規満期 cohort・contract レベルの nomination contract 変更）がある場合に限る。
- **語彙は窓ごとに決めてから全体へ畳む。** 窓を跨いで「どれか 1 つでも被覆不足なら全体 `insufficient`」とすると、1 窓の 1 basis の件数不足が、他窓で確定した効果の不成立を語彙の上で覆い隠す。各窓を `insufficient` / `inconclusive` / `negative` / `adoption_candidate` へ落としたうえで統合し、全体を `insufficient` と呼ぶのは、**効果が確定した窓が 1 つも無い**ときに限る。
- **満期済み窓を根拠に cleanup するときは、残る変動幅を示す。** as-of 範囲が固定で満期済みでも値は不動ではない。forward row は build のたびに再計算され、FY 配当や退場銘柄の exit が backfill されれば `total` basis の pair 数と中央値は動く。したがって次の bullet の `insufficient` 保持規則より削除を優先してよいのは、**その窓の効果が確定しており、かつ窓内の coverage backfill では結論が反転しないことを示した**ときに限る。示せないなら保持規則が優先する。
- `negative` / `inconclusive` が確定した軸は、判定 PR で panel 列・派生計算・評価枝・専用 test を削除し、dated report と git history を反証証跡の正本とする（残すのは `adoption_candidate` / `insufficient` / control 再利用列 / production annotation 入力列のみ）。
- rules variant の計測は本番 rules を変えず `SCREENING_RULES_PATH` で variant を指し、別 store（`stores/screening/calibration/variants/<variant>/`）へ panel を構築する。rules_hash provenance が混線を機械検出する。
- 機械レバー（approach / 候補一覧 / E[r]）の実証的改訂は 3y/5y eligible evidence を必須の関門にし、判断レバー（市場環境判断 / research 手順）は保有 outcome と運用の事後検証で改める。

### 採用後

以下は実証的な機械method変更を採用した場合の手順である。文書だけの変更でrun、候補一覧、Research selection、較正artifactを再発行しない。

- 通過した変更だけを本番へ反映し、計測した構成と本番構成を一致させる。rules 改訂後は panel を `--force` 再構築する。
- 現 asofの`screening run`が全分析と候補文脈を一度に確定する。返されたrun revisionを`screening candidates show --run-revision-id <RUN_REVISION_ID>`で読み、dated method revisionのApproach別理由とexact unionを実銘柄で確認する（運用テスト）。
- `reports/studies/YYYY-MM-DD-<slug>/report.md` に再現手順・データ窓・coverage / survivorship 開示・判定表・検算・採用後の監視事項を固定する（一次計測記録。別の監査ファイルは作らない）。マージ前 gate は [`python-foundation.md`](./python-foundation.md) §9 が正本。マージ後は report の監視事項を次の replay 計測で追う。

### 判断コホートの集計

Research Setの対象について、[Stock Assessment](./stock-assessment.md#scenario-arithmetic)の期間別終価・今後分配・要求年率で割り引いた現在価値と、[機械anchor](./valuation-metrics.md#machine-valuation)は異なる評価座標である。数値差や乖離率を比較するのは、同一基準日、株式単位、対象期間、分配の扱い、valuation methodが比較可能で、終価・分配込み価値・現在価値のどれを比較するか明示できる場合に限る。現在fundamentalsを固定した倍率anchorを将来終価や分配込み現在価値と同一視しない。条件が異なる場合は差の数値比較を避け、期間・分配・評価方法などの方法差として説明する。

比較可能な有効観測（同じStock Assessmentの再利用・scaffold-only・未Review・遡及記入を除く）が5件以上になったら、保存済み評価と機械anchorを読み、乖離率の中央値・範囲・要因件数・coverageを記述する。比較不能な対象は理由とともにcoverageへ区別する。この記述だけでscreening式を変えず、変更仮説は別Issueで事前登録してdesign/confirmへ進める。

### 支配権イベントの exit 代理値

上場廃止で市場終値が無くなった forward 窓は、成立した現金公開買付けの 1 株買付価格を代理値として計算する（`resolved_control_event_exit`）。買付価格と成立報告は観測事実だが、上場廃止日にその価格で退出したという扱いは較正上の測定規約である。非応募株主のスクイーズアウト対価、実際の支払日、個々の受取額を確認したものではない。`resolved` はこの規約で計算できたことを表し、実約定・現金受領の証明とは区別する。[JPXの上場制度FAQ](https://www.jpx.co.jp/faq/stock_listed_company.html)も、非応募株主の後続手続について発行会社の開示確認を案内している。導出規則・置換規則・比較方法・停止条件は [`reports/studies/2026-08-11-capital-control-exit-values/preregistration.md`](../../reports/studies/2026-08-11-capital-control-exit-values/preregistration.md) に事前登録し、置換前後の較正影響を同ディレクトリの report に固定する。代理値を確定できない上場廃止は従来どおり全損・中立の感度ケースに残り、`unpriced_exit_flips_direction` の判定材料であり続ける。

JPX理由の「公開買付」「株式の併合 / 株式併合」「株式等売渡請求」は資料確認へ進む候補条件で、現金対価の証明ではない。株式併合・売渡請求だけの表記でも、先行する現金公開買付けを資料で検証する。対象ticker・届出/訂正・取下げ・成立報告・普通株1株の単一価格・現金のみという既存の証拠条件を保ち、資料なし・撤回・非現金・価格不一致は未解決に残す。CLIの`candidates`は調査候補件数、`resolved`が証拠を満たした件数である。6881の通常資料取得による再現と取得範囲は[測定記録](../../reports/studies/2026-10-14-tender-exit-candidate-coverage/report.md)を参照する。

### 破綻型の実現 exit 値

上場維持基準への不適合・破産・民事再生・会社更生・債務超過・内部管理体制・開示義務違反による上場廃止は、市場が最後に付けた終値で解決する（`resolved_failure_exit`）。これは最終市場価格を使う測定規約であり、実際の換金額・最終回収額や株式価値ゼロを確認したものではない。この分類に含まれる上場廃止理由だけで倒産や無価値を認定しない。

破綻型の分類はJPXの廃止理由に基づき、買収と区別できない場合は未解決に残す。買収を破綻型と誤読するとプレミアムを失う一方、分類不能を未解決に残せば既存の欠落として開示できるためである。語句と除外条件の完全な定義は[forward](../../engine/src/baibai_engine/screening/calibration/forward.py)を参照する。`株式の併合`だけでは破綻と判定しない。

価格は entry と同じ調整系列の終値どうしなので、買付価格と違い基準の突合を要さない。調整 factor の被覆は `resolved` と同じく行に記録し gate はしない。退出日が上場廃止日より後の銘柄（廃止後に再び取引された系列）と、1 窓に破綻型の廃止が 2 件入る銘柄は実値化せず unresolved に残す。長期売買停止では最終終値と最終回収価値が乖離し得る。乖離の方向・大きさは廃止理由だけでは確定できず、この価格を実際の売却代金として扱わない。

### 誠実性の規律

較正は3か月以上のhorizonを対象にし、3m/6mはregression alert、1yはleading evidence、3y/5yは実証的method変更のevidenceに使う。詳細な必須条件は本書「Horizonごとのevidence role」に従う。

重複するcohortを独立標本とせず、有意性・統計的優位ではなく効果量とcohort勝率を読む。仮説と採否基準は前記の事前登録・design/confirmで扱い、grid searchをしない。coverage・survivorshipの欠けを開示し、較正結果を累積return・年率・シャープ等の運用実績として掲げない。
