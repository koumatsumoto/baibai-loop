# 見積り較正

保存入力から過去as-ofの機械見積り・候補を再構成し、forward outcomeと照合する。資産全体の運用成績やL3銘柄評価の正しさとは別の横断診断である。生成・評価は`machine_analysis/screening/calibration`、保存文脈の意味と利用可否は`machine_analysis/company_valuation/calibration`が所有する。

## 何を測るか

3m/6mはregression alert、1yはleading evidence、3y/5yは実証的method変更の根拠に使う。通常evaluateはdiagnostic。empirical_change_evidenceでは対象月・decision subjectを明示し、必要metricと入力・結果・methodの整合を確認する。evidence completeは条件が揃う意味で、効果が正・採用すべきという判定ではない。

Approachを評価する時はその設定depth全件とunion、対象Approach/unionの再現を測る。unionの評価は全4Approachのfidelityを要する。固定top5/10/20は設定された全推薦集合と別で、将来outcome欠測を理由に下位を繰り上げない。必須metricはsubjectから導き、required-metricは事前に選んだ追加guardrailだけに使う。

月次gridは保存観測の条件を満たす月内最終日。指定endや月labelは公式月末営業日・全銘柄取得完了の保証ではない。horizonは実as-ofから暦月/年で進め、対象日以下の取引日へ解決する。entry許容窓により実保有期間は名目より長くなり得る。

## 入力と欠測

同日masterと当時の母集団を区別する。prior/future snapshotは診断として明示できても、正式な過去母集団へ昇格しない。survivorship不完全の集合でexit/factorがcompleteに見えても、既に落ちた銘柄の不備は観測できない。

coverageは成功した取得範囲を示し、行のmin/max・manifest件数や現在のprovider契約から創作しない。保存入力の主窓・補助FY履歴・master・EDINET軸・退出/配当の窓を分ける。過去EDINET軸の0は未観測で、本番の全入力面を再現した意味ではない。古いcohortほどrolling下限で履歴が短く、nullを古い数値で補わない。

forward未解決は未価格entry、既知価格を持つpanelとのentry gap、未成熟、退出不足等を区別する。entry rowなしとclose/volume nullも異なり、どちらも未上場・停止・無約定理由を単独で確定しない。未計測を「欠けなし」にしない。

未価格exitとpriced-master未評価の感度は、対象を同定して全損・中立の固定caseへ置き換え、報告値を含む結論方向が必要metric自身で安定するかを見る。これは欠けた実現値の復元や可能な退出価値を囲む上下界ではない。目的外metricの反転を対象metricの不成立へ流用せず、共有入力の不備・未知statusは別に残す。件数だけで一律に成立/不成立を決めない。

## priceとtotal

相対ER較正は価格収束成分とprice-only実現値の横断spreadを比べる。carryは同じbasisで観測されないため絶対ER水準の検証にはならない。price/totalで評価可能母集団が異なれば、中央値の差を配当効果と解釈しない。

FY実績配当込みは期間内に期末を迎えた対象年度のDivAnnを共通価格basisへ揃える。支払日・権利落ち日のcash ledgerではなく、端のFYを月割りもしない。entry利回り×年数を実現配当として足さない。配当0は観測、欠損・期間gap・basis不明は未解決。rights issueの権利売却/払込cashは価格factorだけで復元できず、価格だけの成績を残してtotalを未解決にする。

グロス株数変化は将来buyback cashの観測ではない。業種/市場fallback、母集団構成、重複cohortと実効観測数を区別し、月次cohort数を独立標本数として有意性を主張しない。母数不足と測定した効果なしも別である。

## 退出価値の測定規約

成立した現金TOBの1株価格は、廃止後の市場終値がない窓の退出代理値に使う。成立と価格は観測事実でも、廃止日にその値で退出した扱いは測定規約。実約定・受領日・非応募者の最終対価の証明ではない。JPXの買収/併合等の理由は資料確認の入口で、現金対価の証明にしない。対象・訂正・取下げ・親案件・成立・株式basisを確定できなければ未解決に残す。[導入比較](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-11-capital-control-exit-values/report.md)を参照する。

破綻型理由の退出は最終市場終値で測る。理由だけで倒産・無価値と認定せず、最終回収価値や実売却代金と呼ばない。買収と分類できないもの、廃止後の取引や複数退出で曖昧な窓は未解決に残す。長期停止による価格と回収の乖離は理由だけでは確定できない。

<a id="er-level-distribution-context"></a>
## 判断へ渡す歴史分布

配布文脈は企業価値モデルに近接する[published YAML](../../engine/src/baibai_engine/machine_analysis/company_valuation/calibration/published/er-level-calibration-latest.yaml)。Gitでreview・同梱する単一artifactで、再生成用のcalibration snapshotとは役割が異なる。正常な結果観測不能では宣言月・母数と理由を保ち、観測分布とformal evidence不成立を別に渡す。panel欠落・集合不整合・未知状態・method不一致をpartialと呼んで許容しない。

結果が観測できた集合から予測帯を作らず、宣言した有限ER全対象から固定境界を求め、同じ境界で両horizon・basisと現在候補を分類する。未評価を0損益へ入れず、観測なしの分布はnull。ticker-asof等重みとcohort等重み、宣言/観測月数、各母数を区別する。固定8.5%帯は比較座標で、[L3の要求率](../doctrine.md#required-return-policy)に連動させない。

候補への対応帯は、実際のoperative runのrules/model identityと一致した場合だけ読む。artifactは生成後45日の範囲と有効期限を持ち、未来・期限切れ・schema/basis/band不正・不一致は利用不能。Investment Judgment全体の新hard gateにせず、順位・FV・保存入力・要求率を変更しない。分布は観測済みの歴史で、全母集団の実証済期待収益や個別予測ではない。

必要時だけinvestment-judgment calibration-contextで固定Operationを指定する。古いrunへ現行artifactを付け替えず、不足・formal completenessと利用不能理由を合わせて読む。YAMLを手編集して生成日・hashだけ更新しない。

固定v43標本には粗い順位の研究材料があるが、未解決対価・配当basis・過去入力/PITと実行可能価格の制約が残り、期待リターンの数値較正や買い閾値には採用しない。少数の公式原本を回復できても全面backfillの便益は未確認で、目的と必要入力を限定する。手計算感度でformal gateを上書きしない。[v43の採否](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-23-v43-evidence-decision/report.md)と[回復pilot](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-23-historical-recovery-pilot/report.md)に詳細を残す。

[再生成](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-21-current-calibration-context/report.md)と[対象metric別の再評価](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-22-decision-quality-closure/report.md)は当時の入力・方法の記録である。過去artifactのbytesと当時の方法を現行値へ付け替えない。

## methodと再構築

productionと較正は同じ丸め済みStock Analysisと候補純関数を使う。rules/method/calculation revisionや入力窓・forward規約が異なるcacheを読み替えず、現在入力から再構築する。意味変更はownerのrevisionへ反映するが、文書・配置・importだけで進めない。field順や無効knob削除でも現行rules hashが変わる場合があり、再構築費用と便益を比較する。

calibrationは再生成可能なローカルsnapshot。generation graph・remote pointer・過去世代保持を持たず、temporary buildからatomic replaceする。全再構築は保持する全cohortを覆う範囲とforceを指定し、先に現snapshotを消さない。forceは指定範囲だけから新snapshotを作る。不足coverageは再実行だけでは解消しない。

固定lake releaseを空SQLiteへhydrateしてもstore-local source_coverageは復元されない。同じorigin/releaseの通常market取得と組み合わせ、別世代seedを混ぜない。行数やmin/maxから取得記録を生成しない。隔離コピー・sealed snapshotの通常容量guardを緩めない。

### pre-2019診断

pre2019_self_range_375は375-session self-rangeと600暦日bar窓の診断専用variant。別calibration directoryを必須とし、variantと窓をidentityへ含める。productionの750-sessionと違うためformal empirical evidenceへ使えない。実効観測本数も確認し、短い履歴をfull-windowとしない。この診断契約は維持する。

操作例は[較正の運用](../../jobs/OPERATIONS.md#定期maintenance)に置く。

## 方法を変える判断

計算の正誤修正は算術・期間・単位・株式basis、運用不具合は成功/失敗経路で検証する。文書や配置整理にStudy・本番再生成を要求しない。経済仮説の優劣を主張する変更は計測前に仮説・採否基準・対象をIssue/PRへ固定する。

効果を読まずに測れる被覆・membership・集中の不足は先に確認する。結果を読んだ後に条件を動かさず、時間でdesign/confirmを分け両方の方向と基準で採否する。全cell通過を無条件の既定にせず、noise floor等の判断を計測前に置く。既知結果への接触や残るcoverageの限界も示す。

窓ごとにnegative/insufficient/adoption_candidate/inconclusiveを判断してから統合し、他窓の不成立を一つの被覆不足で隠さない。insufficientは効果なしではない。一方、用途のないpanel列・評価枝・専用testを、成熟待ちだけで保持する義務はない。[開発原則](../doctrine.md#development-investment-policy)から維持費と価値を判断する。

採用は計測した構成と一致させ、必要範囲を再buildする。個人の保有成果と機械replayを合算せず、機械手法の実証は3y/5y、判断手順は実運用と保存評価の事後照合から改める。銘柄評価と機械anchorの数値差を比べる時も、基準日・株式単位・期間・分配・評価方法を揃える。比較不能を数値の優劣へ変換しない。

実行・月次更新は[OPERATIONS](../../jobs/OPERATIONS.md#定期maintenance)へ進む。記述分布生成の成功を方法変更の根拠成立や運用利益の証明として掲げない。
