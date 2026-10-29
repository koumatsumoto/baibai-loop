# 見積り較正

現行の較正実装は旧ER/FV・四Approachを対象に、保存入力から過去as-ofを再構成しforward outcomeと照合する。新しい経済経路の候補政策を較正済みと扱わず、新Selectionへ旧ER分布を付けずnot_applicableを返す。資産全体の運用成績やL3銘柄評価の正しさとは別の横断診断である。実装の入口は[較正処理](../../engine/src/baibai_engine/machine_analysis/historical_evaluation/)と[保存文脈](../../engine/src/baibai_engine/machine_analysis/historical_evaluation/context/)。本書は測定値の解釈と採否の制約を残し、CLI・field・検証分岐は転記しない。

<a id="machine-valuation"></a>
## 保存済み機械ER/FVの意味

旧runの機械E[r]は倍率収束とcarryによるsecondary prior。以下は保存済み結果と旧較正を読むための意味であり、新しい候補抽出・L3入力には含めない。AI調査の優先度や企業固有の価値を所有しない。年率ratio、成分、入力basis、anchor・係数・clipの前提と一緒に読む。機械の5年priorは銘柄評価の期間を固定しない。

carryの配当・グロス株数変化が欠測なら計算上の寄与は0になるが、無配・株数不変を観測した意味ではない。元のnullable入力と欠測仮定を示す。予想DPS spike guardは一回性の分配を反復carryへ入れる影響を限定するため採用したが、当時の長期比較は盲検の閾値検証ではなく、5y top5 trap悪化の受容を含む。[採用時の制約](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-30-dividend-carry-spike-guard/report.md)は将来成績の保証ではない。株数減少は消却・発行等を含む過去の変化で、自己株取得の実行cash・未消化枠・将来還元ではない。

業種中央値anchorと自己レンジanchorは機械比較値で、企業の期間内分配・将来終価・現在価値とは別である。Stock Assessmentへ重複転記せず、保存run・時点とともに引用する。保存fieldの不在、明示null、0を混ぜず、旧snapshotへ後付けして再発行しない。

保存済み照会は選択runの値を読む。論理as-of cutoffで選ぶrunは後日生成した過去replayもあり、当時既知の保証ではない。対象runなし・run内tickerなし・計算不能を区別し、古い値や候補一覧で埋めない。企業の条件付き算術は[Stock Assessment](stock-assessment.md#scenario-arithmetic)、歴史分布は本書の較正。

<a id="carry-policy"></a>
### 機械priorの採否

carryはグロス株数変化を維持する。自己株控除後の変化は別のfactとして残すが、当時の比較はrank ICが劣り、分位差も時間分割で反転し、置換を支持しなかった。較正の実現率引上げとupside/buyback cap拡張も両horizonの改善を確認できず採らない。これは将来の有効性を否定する判定ではない。詳細は固定した[株数比較](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-17-tradable-share-change/report.md)、[実現率](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-01-er-level-calibration/report.md)、[cap比較](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-10-er-distribution-context-v2/report.md)に残す。

## 何を測るか

短期は回帰警報、1yは先行材料、3y/5yは手法変更の根拠として区別する。通常の診断出力と、事前に対象・必要metricを定めた方法変更の証拠は別である。証拠のcompleteは測定条件が揃う意味で、効果が正・採用すべきという判定ではない。

評価対象の推薦集合を先に固定し、固定top-kと全推薦のunionを混同しない。将来outcomeの欠測を理由に下位を繰り上げたり、結果を見た後で評価対象やguardrailを選び直したりしない。

月次gridは保存観測の条件を満たす月内最終日。指定endや月labelは公式月末営業日・全銘柄取得完了の保証ではない。horizonは実as-ofから暦月/年で進め、対象日以下の取引日へ解決する。entry許容窓により実保有期間は名目より長くなり得る。

## 入力と欠測

同日masterと当時の母集団を区別する。prior/future snapshotは診断として明示できても、正式な過去母集団へ昇格しない。survivorship不完全の集合でexit/factorがcompleteに見えても、既に落ちた銘柄の不備は観測できない。

coverageは成功した取得範囲を示し、行のmin/max・manifest件数や現在のprovider契約から創作しない。保存入力の主窓・補助FY履歴・master・EDINET軸・退出/配当の窓を分ける。過去EDINET軸の0は未観測で、本番の全入力面を再現した意味ではない。古いcohortほどrolling下限で履歴が短く、nullを古い数値で補わない。

forward未解決は未価格entry、既知価格を持つpanelとのentry gap、未成熟、退出不足等を区別する。entry rowなしとclose/volume nullも異なり、どちらも未上場・停止・無約定理由を単独で確定しない。未計測を「欠けなし」にしない。

未価格exitとpriced-master未評価の感度は、対象を同定して全損・中立の固定caseへ置き換え、報告値を含む結論方向が必要metric自身で安定するかを見る。これは欠けた実現値の復元や可能な退出価値を囲む上下界ではない。目的外metricの反転を対象metricの不成立へ流用せず、共有入力の不備・未知statusは別に残す。件数だけで一律に成立/不成立を決めない。

## priceとtotal

相対ER較正は価格収束成分とprice-only実現値の横断spreadを比べる。carryは同じbasisで観測されないため絶対ER水準の検証にはならない。price/totalで評価可能母集団が異なれば、中央値の差を配当効果と解釈しない。

FY実績配当込みは期間内に期末を迎えた対象年度のDivAnnを共通価格basisへ揃える。支払日・権利落ち日のcash ledgerではなく、端のFYを月割りもしない。entry利回り×年数を実現配当として足さない。配当0は観測、欠損・期間gap・basis不明は未解決。rights issueの権利売却/払込cashは価格factorだけで復元できず、価格だけの成績を残してtotalを未解決にする。

期末の欠落は既知の会計期間境界から判定する。exit後の最初のFYも、直前の対象年度が欠けている証拠として開始日だけを使えるが、その配当額を期間内へ加算しない。後続資料や境界が不明なだけで12か月決算を仮定して欠落とせず、短期・変則決算と区別する。

グロス株数変化は将来buyback cashの観測ではない。業種/市場fallback、母集団構成、重複cohortと実効観測数を区別し、月次cohort数を独立標本数として有意性を主張しない。母数不足と測定した効果なしも別である。

## 退出価値の測定規約

成立した現金TOBの1株価格は、廃止後の市場終値がない窓の退出代理値に使う。成立と価格は観測事実でも、廃止日にその値で退出した扱いは測定規約。実約定・受領日・非応募者の最終対価の証明ではない。JPXの買収/併合等の理由は資料確認の入口で、現金対価の証明にしない。対象・訂正・取下げ・親案件・成立・株式basisを確定できなければ未解決に残す。[導入比較](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-11-capital-control-exit-values/report.md)を参照する。

破綻型理由の退出は最終市場終値で測る。理由だけで倒産・無価値と認定せず、最終回収価値や実売却代金と呼ばない。買収と分類できないもの、廃止後の取引や複数退出で曖昧な窓は未解決に残す。長期停止による価格と回収の乖離は理由だけでは確定できない。

<a id="er-level-distribution-context"></a>
## 判断へ渡す歴史分布

配布文脈は企業価値モデルに近接する[published YAML](../../engine/src/baibai_engine/machine_analysis/historical_evaluation/context/published/er-level-calibration-latest.yaml)。Gitでreview・同梱する単一artifactで、再生成用のcalibration snapshotとは役割が異なる。正常な結果観測不能では宣言月・母数と理由を保ち、観測分布とformal evidence不成立を別に渡す。panel欠落・集合不整合・未知状態・method不一致をpartialと呼んで許容しない。

結果が観測できた集合から予測帯を作らず、宣言した有限ER全対象から固定境界を求め、同じ境界で両horizon・basisと現在候補を分類する。未評価を0損益へ入れず、観測なしの分布はnull。ticker-asof等重みとcohort等重み、宣言/観測月数、各母数を区別する。固定8.5%帯は比較座標で、[L3の要求率](../doctrine.md#required-return-policy)に連動させない。

候補への対応帯は、実際のoperative runのrules/model identityと一致した場合だけ読む。artifactの有効期間と構造・basisを所有実装で検証し、未来・期限切れ・不一致を利用可能としない。Investment Judgment全体の新hard gateにせず、順位・FV・保存入力・要求率を変更しない。分布は観測済みの歴史で、全母集団の実証済期待収益や個別予測ではない。

必要時だけinvestment-judgment calibration-contextで固定Operationを指定する。古いrunへ現行artifactを付け替えず、不足・formal completenessと利用不能理由を合わせて読む。YAMLを手編集して生成日・hashだけ更新しない。

固定v43標本には粗い順位の研究材料があるが、未解決対価・配当basis・過去入力/PITと実行可能価格の制約が残り、期待リターンの数値較正や買い閾値には採用しない。少数の公式原本を回復できても全面backfillの便益は未確認で、目的と必要入力を限定する。手計算感度でformal gateを上書きしない。[v43の採否](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-23-v43-evidence-decision/report.md)と[回復pilot](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-23-historical-recovery-pilot/report.md)に詳細を残す。

[再生成](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-21-current-calibration-context/report.md)と[対象metric別の再評価](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-22-decision-quality-closure/report.md)は当時の入力・方法の記録である。過去artifactのbytesと当時の方法を現行値へ付け替えない。

## methodと再構築

旧run経路と旧較正は同じ丸め済みStock Analysisと候補純関数を使う。新しいモデル生成・二view政策とは別契約であり、旧較正の成績を新政策へ転用しない。rules/method/calculation revisionや入力窓・forward規約が異なるcacheを読み替えず、現在入力から再構築する。意味変更はownerのrevisionへ反映するが、文書・配置・importだけで進めない。field順や無効knob削除でも現行rules hashが変わる場合があり、再構築費用と便益を比較する。

calibrationは再生成可能なローカルsnapshot。generation graph・remote pointer・過去世代保持を持たず、temporary buildからatomic replaceする。全再構築は保持する全cohortを覆う範囲とforceを指定し、先に現snapshotを消さない。forceは指定範囲だけから新snapshotを作る。不足coverageは再実行だけでは解消しない。

固定lake releaseを空SQLiteへhydrateしてもstore-local source_coverageは復元されない。同じorigin/releaseの通常market取得と組み合わせ、別世代seedを混ぜない。行数やmin/maxから取得記録を生成しない。隔離コピー・sealed snapshotの通常容量guardを緩めない。

### pre-2019診断

pre-2019の短窓variantは、標準の必要履歴がない区間を調べる診断用であり、標準手法の実証へ転用しない。別の作業snapshotとidentityに分離し、短い履歴をfull-windowと呼ばない。窓・識別子・検証条件は較正実装が所有する。

## 方法を変える判断

計算の正誤修正は算術・期間・単位・株式basis、運用不具合は成功/失敗経路で検証する。文書や配置整理にStudy・本番再生成を要求しない。経済仮説の優劣を主張する変更は計測前に仮説・採否基準・対象をIssue/PRへ固定する。

効果を読まずに測れる被覆・membership・集中の不足は先に確認する。結果を読んだ後に条件を動かさず、時間でdesign/confirmを分け両方の方向と基準で採否する。全cell通過を無条件の既定にせず、noise floor等の判断を計測前に置く。既知結果への接触や残るcoverageの限界も示す。

窓ごとにnegative/insufficient/adoption_candidate/inconclusiveを判断してから統合し、他窓の不成立を一つの被覆不足で隠さない。insufficientは効果なしではない。一方、用途のないpanel列・評価枝・専用testを、成熟待ちだけで保持する義務はない。[開発原則](../doctrine.md#development-investment-policy)から維持費と価値を判断する。

採用は計測した構成と一致させ、必要範囲を再buildする。個人の保有成果と機械replayを合算せず、機械手法の実証は3y/5y、判断手順は実運用と保存評価の事後照合から改める。銘柄評価と機械anchorの数値差を比べる時も、基準日・株式単位・期間・分配・評価方法を揃える。比較不能を数値の優劣へ変換しない。

実行・必要時の更新は[OPERATIONS](../../jobs/OPERATIONS.md#定期maintenance)へ進む。記述分布生成の成功を方法変更の根拠成立や運用利益の証明として掲げない。
