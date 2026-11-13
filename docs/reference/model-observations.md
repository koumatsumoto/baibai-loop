# 観測と原典の読み方

ここは観測の意味と限界を扱う。層/保存は[architecture](../architecture.md)、候補は[Screening](screening-runtime.md)、判断は[Stock Assessment](stock-assessment.md)、取得/復旧は[OPERATIONS](../../jobs/OPERATIONS.md)へ。算式/fieldは実装を正本とする。

<a id="source-owners"></a>
## 原典・時点・不足

- **原典**: 原公表機関・公式集約先・企業開示・確認済みbroker事実を使い、報道は補助とする。source tierは正確さ/許諾を保証しない。有料媒体を使わず、契約内の個人・非公開利用に限る。原文/転載表を公開Issueへ再配布しない。
- **時計**: 対象日/会計期、公表/訂正、実受領、入力cutoff、評価時刻を分ける。未知日を受領日で埋めず、Web Archiveの原URL/captureも当時公表の証明にしない。保存入力identityをas-of/latest headで代用しない。固定snapshotは未保持vintage/intradayまでの完全PITではない。
- **比較**: 期間・会計scope・通貨/単位・株式basisを揃え、原値・定義・時計・参照を保つ。加工/転載/再受領を独立証拠、有限fixture/coverageを全市場・将来収益の実証にしない。後日訂正と当時既知を分ける。
- **不足**: 未取得・失敗・取得済み空・partial・正常null・比較不能・明示0を分ける。新期/訂正の不足を旧版・別期・別basisで埋めず、影響を依存値へ局所化する。障害を不存在、固定object破損を正常欠測にせず、原額/独立観測を残す。
- **代替**: 継続障害時に期間・定義・値が複数経路で一致した場合だけ、一次未確認を明示して二次配信を採る。単一値・不一致・一時障害で代用せず、AI転記でstoreを埋めない。実装・受領・公開・切替・計算可能は別である。

[経済catalog](../../engine/src/baibai_engine/data_collection/catalog.yaml)は経済の取得範囲で全L1一覧ではない。他sourceの入口は[取得/復旧](../../jobs/OPERATIONS.md#sources)を参照する。

<a id="financial-periods"></a>
## 開示と会計比較

普通株は[J-Quants原本](../../jobs/OPERATIONS.md#jquants-native-operations)の完全code/商品区分で識別し、末尾0/業種名から推定しない。calendar元区分もbooleanから復元しない。JPXは選択完了bundleを使い、旧行を混ぜず、消失を取消/不存在、受領完了を同時公表としない。過去の決算予定は会社IRで再確認する。

[開示選択](../../engine/src/baibai_engine/machine_analysis/observations/financials/financial_documents.py)は期/連結区分/基準を先に定め、訂正/競合をfield単位で扱う。番号・投入順・未知時刻で順序を作らず、空欄を撤回、過年度訂正を現在区分変更としない。時刻不明だけで不変観測を捨てず、flag/空一覧/修正再表示「無」だけで比較可否を決めない。未確定basisを旧FYで迂回しない。

累計・単独四半期・TTM・期末残高を分ける。EPS/平均株数/営業CFの四半期差分や、異分母EPSの加減算、丸め/basis不明EPSからの総額復元はしない。計算は[財務owner](../../engine/src/baibai_engine/machine_analysis/models/company_financials/metrics/)へ。

- **財務/成長の違い**: 財務TTMの最新額合成と成長の累計訂正制約は別契約。配当/予想文書で実績を消さず、予想混入/単一四半期の年率化はしない。前年累計の配信がFYより後でも原資料は先行し得る。
- **成長の窓**: 会計期末の表示窓外の比較元/訂正も使い、窓変更で共通期の意味を変えず読取から取得しない。売却/M&A/為替/会計方針を組替済みの有機成長とは呼ばない。組替値の遡及供給は保証されず、重要差はL3が原典で評価し原観測を保つ。
- **利益/倍率**: 報告利益のYは分配可能利益/期待収益率ではない。連結の親会社帰属利益/単体純利益、現在資本basisのEPS/会社平均株数EPS、会社予想/consensusを分ける。優先配当等を推定せず、予想利益の大小や赤字注記だけで持続性を決めない。
- **変化**: 赤字縮小/黒字化は額と符号で読み、負/0基準の率で改善を決めない。営業利益の経常利益代用、金融業への非金融利益率適用、未検証basisの変化率を避け、成長モデルが持たないPER/peer比較で補わない。

<a id="reported-comparisons"></a>
### 原本内の比較と組替

EDINETは同じmarket sealに適格な任意入力で、未取得でも全社生成/J-Quantsを止めない。CSV/XBRL、原本受領/抽出を分け、[保持/再開](../../jobs/OPERATIONS.md#edinet-list-native-l1)に従う。再抽出で初回bytesを差し替えず、提供元修復後も隔離取得/差分確認を要する。

[comparison再利用](../../engine/src/baibai_engine/machine_analysis/generation/comparison_evidence.py)は任意cache。同じ原本/抽出入力・revisionに一致する適格入力だけ再利用し、旧/不正形式なら再抽出する。原本の取得/保持を代替せず、cold再生成に前回L2を要しない。

EDINETでは明示された日本/米国基準もIFRS抽出の確定的な非対象として再利用するが、空/未知基準・観測全体の欠測/解析失敗は再試行する。成功観測内の個別operand欠測は、その理由を保って再利用できる。旧来の非対応理由から確定非対象を推定しない。いずれも今回の固定原本を読み存在・hashを検証し、現在の文書/訂正選択を優先するため、保存原本のI/Oは省かない。

| 経路 | 固有の成立範囲 |
| --- | --- |
| [EDINET同一文書](../../engine/src/baibai_engine/machine_analysis/observations/financials/edinet_comparisons.py) | IFRS年次/半期本表の当期・比較期を別系列へ保存する。J-Quants TTM/倍率へ横結合せず、単体利益を親利益、相対年度名を比較日に読み替えない。 |
| [PDF組替](../../engine/src/baibai_engine/machine_analysis/observations/financials/pdf_selection.py) | 最新期/訂正を照合した同一基準の比較欄/TTM。日本基準の遡及変更は同日公表の組替済み前期実績を要し、表示丸めの近似/不明を精密XBRL値としない。参考実績は年度の訂正原本ではなく、四半期とは別の公表時計で照合する。後の年次訂正や公表順不明を再確認済みとしない。 |

同日IFRS年次operandも、そのPDFに一致するnative文書の時計で四半期以前を確認する。額不変で旧PDFを使う場合は、後のEPS訂正の時刻を借用しない。

訂正系列を保ち、組替成功でも未受領訂正や金融業/業種不明の利益率制約を解除しない。[fixture](../../tests/fixtures/edinet/comparisons/README.md)の年次/半期をQ1/Q3・全本表へ、PDF受領をXBRL取得へ一般化しない。

<a id="capital-basis"></a>
### 資本・価格・配当の単位

発行済総数/平均株数、普通株自己資本/非支配持分込み純資産を分ける。自己株控除は同じ資本状態で行い、新しい自己株欠測を旧状態で補って処分/消却を推測しない。

PBRの`book_equity`は実際に選んだ分母・算式・元開示を示す。自己資本比率経路と、as-of株式basisへ換算したBPS×現在株数経路を区別し、別sourceのEDINET連結scopeや最新開示日を簿価へ付け替えない。出典があっても現在の普通株持分・平均資本ROE・換金価値を保証せず、旧世代に出典を補作しない。

EDINETの金融残高は、簿価や予想と別の出典・連結範囲で読む。現預金と現金同等物の選択科目は旧metricで保持されず、年度metadataはBS実測日ではない。金利負債は抽出科目の合計で、注記だけにある内訳の網羅を保証しない。net cashや投資有価証券だけでは全負債・非支配持分・必要資金の控除や換金可能性を確認できず、普通株へ配れる額にしない。持分法投資と持分利益、金融資産とその運用益の重複は[資本bridge](stock-assessment.md#capital-bridge)で判断する。

期末株数と遡及分割EPS/BPSの混在は[資本補正](../../engine/src/baibai_engine/machine_analysis/models/company_financials/metrics/capital.py)の裏付けで扱い、数値一致だけで誤転記にしない。連続する分割の一部だけが反映済みの場合も、各per-share値と独立総額で既知の株式basisを照合し、複数basisが成立するfieldは未解決にする。JPX PDFの転換も完了と普通株basisを確認し、新発行済数と旧自己株を混ぜない。 後発事象による後日開示は明示効力日と公表日を分け、公表日前のas-ofへ戻さない。取得済み原本の実受領cutoffも別に保ち、予定・複数完了・不一致は株数の推定に使わない。

大量保有報告は第三者申告で、発行会社確認/変更日確定ではなく、時価総額/順位を自動補正しない。受領UTC日だけなら翌日以降、評価前の固定snapshotがある場合だけ同日その固定時刻まで利用できる。通常生成は同じ固定EDINET pairのnative在庫/selected receipt、旧SQL/body-only readerはSQLの在庫/受領日を使う。latest確認や精密時計でcoarse dayの採否を広げない。

[発行会社の資本注記](../../engine/src/baibai_engine/machine_analysis/observations/financials/edinet_issuer_capital_reader.py)は最新年次/訂正の同じ普通株表の期末表示。提出日contextや議決権表を接合せず、単位・scope・原文位置を保つ。信託/分割注記は未照合で、現在株数/価格分母の補完や、最新訂正の欠測から旧数値の復活に使わない。

[価格換算](../../engine/src/baibai_engine/machine_analysis/observations/prices/price_basis.py)はraw closeを同じ基準へ揃え、別取得世代の調整価格をつながない。rights issueを含む価格調整と、確定分割/併合だけの株数/配当換算を分け、価格から株数を逆算しない。

実績DPSは期/原単位/分割を確認し、不明actionを解決済みにしない。四半期末を基準日、総額÷期末株数を換算値とせず、相殺で変更を消さない。無配/未解決を分け、欠年度を飛ばして増配streakを復活させない。予想/原額を保ち、旧carryを実現配当としない。適用年度の配当訂正に年間額がなければ旧予想を復活させず、翌年度slotの訂正は翌年度の支払期情報で対象を確認する。

### EDINET金額を評価へ使うとき

[CSV抽出](../../engine/src/baibai_engine/machine_analysis/observations/financials/edinet_metrics/csv.py)の基準/context・0/空欄・合算/内訳を使い、本表とsegment、JP/IFRSを混ぜず、独自concept/類似名から全額を推定しない。

IFRSの金利負債は、流動・非流動の社債及び借入金とリース負債が標準科目で揃う表示を採用する。借入金だけ、その他金融負債、注記本文から不足分を埋めず、合算額へ借入内訳を重ねない。これは抽出できる残高の範囲であり、全債務や普通株への分配可能資金を保証しない。

- **残高**: 現預金と現金同等物を分け、抽出債務を網羅としない。証券は税/拘束/換金性等未反映の帳簿額。net cashは時点残高、非金融のasset-backed ratioはgross調査proxyで清算価値でなく、金融業へ流用しない。
- **EBITDA**: 同書類の対応償却を使い、異なるCF加算子の省略や、のれん償却による減価償却補完をしない。負EV/負EBITDAを分け、負倍率を割安としない。
- **OCF/capex**: 運転資金内訳は年次、標準科目の有形/無形取得支出は年次と中間の部分内訳で、税・契約債務・必要資金を網羅しない。OCF内訳はCFへの寄与符号、設備支出は正の支出額。欠測を0、合算CapExを推定分解せず、J-Quants OCFと混ぜてFCFを作らない。内訳0の感応度を正常FCF/維持投資とせず、原本取得対象は広げない。中間の部分支出だけで完全FCFや年間利回りを作らない。
- **期間**: annual exactは年度metadataで最新TTM/実測context/持続性の保証でない。半期・変則/不明年度を年率化せず、context日を年度/提出日で補わない。短信OCF TTMとの精度差を保ち、異期間FCF/CFOを差し引かない。

短信との規模照合は範囲ずれ検知で同時点/連結範囲の証明でなく、BS時点/debt scopeも開示日・期末・carry lagから推定しない。

<a id="edinet-research-facts"></a>
Company Research factsは年次/訂正と比較年度の任意入力。外部/内部売上・利益basis・区分変更を揃え、提出JST日以前へ遡及しない。JP GAAPの明示元本は社債括弧額を重複せず、リース/利率/担保/covenant/IFRS/5年超残差を網羅しない。抽出ok/0行を負債0とせず、失敗で既存factを消さず、CF/借換推定を混ぜない。

<a id="market-context-reads"></a>
## 価格・市場・業種の比較

各時点以下の最新masterの普通株を使い、除外銘柄を旧snapshotから戻したり母集団を候補へ縮めたりしない。市場breadthはbenchmarkと同じ価格日、個社相対はbenchmark/自己除外peerと両端日を揃える。業種表は採用価格日を保持し、benchmark欠測時は既存の最新価格日fallbackを使う。過去成果に日付がなければ不明のまま読み、as_ofで補わない。業種表の成立はbreadth成功でなく、各社bar窓の中央値も共通開始日を保証しない。補間/業種中央値で端点不足を埋めず、ratio差0.1は10 percentage pointsで割安ではなく、自己価格順位と旧倍率レンジ位置も別である。

不正な価格は依存する比較端点・窓を欠測にし、古い価格日へ繰り上げない。他社や独立した端点比較まで一律に消さない。

個社の`relative_1d/5d/20d/60d`は実在価格bar間の差で、共通営業日数ではない。欠測・停止を挟むと長期間になり得るため、L3初回比較でも`comparison_windows`の両端日を併読する。benchmarkの端点欠測と個社の窓不足を分け、日付のない旧結果へ現在の窓を補わない。

<a id="technical-analysis"></a>
[TA](../../engine/src/baibai_engine/machine_analysis/models/technical_analysis/)は配当なしの調整終値を使う。bar本数は窓・鮮度・流動性・取引可能性を保証せず、慣例期間の最適性、BBの95%包含/反転確率、RSIのbuy/sellを仮定しない。RSIの履歴依存やflat中立値は計算規約である。

共通calendar端点returnと実在最高終値peakからの下落は別で、未観測日の最高値保証/前年比returnとしない。価格なしでは無約定/終日停止を識別できず、row不在/部分欠測/不正値を分ける。実在価格対で注記成立を示せても未知日が不成立確定を妨げ得るため、欠測を一律陽性取消/陰性にしない。

価格注記は研究目安で、採否/自動開始/調査義務ではない。旧MC窓/高値や旧run母集団を現方式で再計算しない。

<a id="margin"></a>
### 信用需給

全銘柄週次/日次と選別subsetのmargin-alertは別母集団。非掲載を0/全銘柄欠測の補完にせず、公表前の空・失敗・対象なしを分ける。公表日と残高日は別で、規制開始日ではない。金額欠測を株数×価格で作らない。

[reader](../../engine/src/baibai_engine/machine_analysis/observations/trading_activity/margin_reader.py)は明示公表日を優先し、なければ週次2/日次1営業日lagを使う。公表当日は含めず、実配信時刻の証明とはしない。株数/出来高は確定分割/併合だけで揃え、換算不能は依存比率/差へ局所化する。同一残高のlong/short比まで消さず、残高÷平均出来高を解消日数予測としない。

<a id="reading"></a>
## 経済Reading

[固定観測とdated規則](../../engine/src/baibai_engine/machine_analysis/generation/runners/market_reading.py)による記述で、regime/合成score/売買signalではない。水準/前年比・窓・観測数・samplingを読み、履歴不足/stale/失敗/分布端を分ける。latestは保存最終観測、公表見込みはcadence/lag推定で、現在値/公式calendarを保証しない。L2系列を原本storeへ複製しない。

暦日を人工補完しない。`recomputed_from_current_store`は後日取得/改訂込みでResearchのknown_atを適用しない。公表vintageを持つsourceだけ対象日で制限し、bulk受領時計で当時既知の履歴を一律に落とさない。

| 原典/観測 | 固有の意味 |
| --- | --- |
| FRED/H.15 | rolling/開始日の限界があり、CSV先頭≠全歴史。変更受領/同値確認の時計は[運用](../../jobs/OPERATIONS.md#経済原本)へ。 |
| CFTC/ECB | long/short・通貨両脚を保ち、net/crossから原値を復元しない。 |
| e-Stat | 季調/頻度/地域を揃え、確報を速報で埋めず、単位codeから基準年を決めない。 |
| BOJ/財務省/投資部門別 | 期間代表日/集計末≠公表/訂正cutoff。 |
| Yahoo close/流動性 | 日足には途中値があり、session/受領時計を読む。終了/過去bar≠公式終値/先物清算値。水曜WALCL・週平均WTREGEN・日次RRPのネット流動性≠同時点残高/投資可能cash。 |
| multpl/松井 | 月次推定を日々の過去currentへ戻さない。松井は同社信用買建だけで全市場/追証人数でない。紹介算式で符号を直さず、年推定/再掲を公表年/鮮度の証明としない。 |

<a id="option-iv"></a>
IVの満期/30日補間は[計算owner](../../engine/src/baibai_engine/machine_analysis/observations/economic_indicators/option_iv.py)へ。SQ直後の欠測≠平穏、term負値≠パニック、skew縮小≠警戒後退、put/call差≠source誤り。緊急証拠金日は指定部の清算値を使う。[過去診断](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-07-31-option-iv-quantiles/report.md)は売買閾値/収益を検証していない。

<a id="external-assessment"></a>
## 外部評価と記事

モデル価格・予想/rating・Quants score・投稿/総合診断を分け、origin/尺度を揃える。初回/欠測回復/人数変更は改善でなく、保存最新値は現在値/最新試行成功でない。受領間比較を自己評価との比較とせず、人数/年表示/過去列から未記載の期末/EPS/履歴を作らない。銘柄同一性のない404/「情報なし」は正常欠測でない。

株マップの銘柄同一性と明示的な未計算表示が成立する場合は、提供元日付・scoreのない`not_computed`観測を元の受領時計で保存する。日付のない未知HTMLや数値との矛盾は解析失敗のまま残す。最新の未計算観測を捨てて過去scoreを現在値へ戻さず、日付のある通常欠測とも区別する。

<a id="tradingview"></a>
保存HTML/SDK応答はnative表現でwire bytesではない。取得単位・拒否・再開は[外部評価](../../jobs/OPERATIONS.md#external-assessment-acquisition)・[TradingView](../../jobs/OPERATIONS.md#tradingview-expectations)へ。

TradingViewの受領JST日/master日を公表日、部分公開を市場代表としない。metric別の通貨・FY/next FY・人数を一括流用せず、実受領cutoff/次取引機会を守る。参考closeは株価の第二正本でない。

<a id="public-articles"></a>
索引/本文受領/投影/主張抽出を分け、見出し/概要/全文/部分の範囲を保つ。抽出待ちの理由・抽出状態にも選択入力の本文範囲を残し、概要の再抽出だけで本文不足が解決するとはしない。BBC World/Business・Fed/ECBの通常取得はRSS見出し/概要までで、本文実読とせず、発言を政策決定としない。取得範囲と[exact回復](../../jobs/OPERATIONS.md#article-acquisition)は運用、判断接続の受入は[#1705](https://github.com/koumatsumoto/baibai-loop/issues/1705)へ。金利CSVを独立測定、XLSXセル投影を図表/書式/計算結果の復元にしない。

<a id="judgment-use"></a>
## 判断への受渡し

Research Selectionは決算予定/規制/現在台帳を網羅するとは限らず、表示拡大では保存入力/生成範囲も増えない。固定旧入力から別Generationを保存する専用CLIは未提供で、現在market公開jobを代用しない。

保存前のselection-contextは省略したモデルの完全結果を`generation_model_ref`の固定Generation・subject・model_idで示す。[既存reader](../../engine/src/baibai_engine/machine_analysis/generation/query.py)の`read_model_inputs`へそのGenerationとsubjectを渡して読む。prepare後の`full_result_ref`はOperation内の保存sliceに限られ、未relatedの業種本文は含まない。専用の共有subject一覧/読取CLIは未提供で、関連を推測して補わない。

外部評価の利益/倍率/資本/期の重要差は原典確認し、銀行のEBITDA/FCF等を全社必須にしない。rating加点/coverage別laneを設けず、[固定8社での不採用](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-22-decision-quality-closure/report.md)も将来効果の否定にしない。Investing.comは変更の補助で、第二の日次consensusではない。

<a id="public-research-reports"></a>
[SRC](https://holistic-r.org/report/)・[フィスコ](https://www.fisco.co.jp/service/report/)・[トウシル](https://media.rakuten-sec.net/)などの事業・成長・競争・懸念に関する見解は、重要な仮説の前提・反証を調べる補助に使う。対象と問いに合う保存済み資料・[記事本文](../../.agents/skills/investment-judgment/SKILL.md#記事の原文へ戻る)から始め、不足時に公開原本へ進む。目標株価/ratingだけで済ませず、著者の因果説明を自分の事業理解・価値実現条件と比べ、判断に関係する差や残る問いを既存Research/Assessmentへ残す。

[根拠の確認](stock-assessment.md#観測と根拠)に従い、実績などの事実・会社説明/計画・著者の独自見解を分け、sponsor調査を独立した裏付けに数えない。公表時点・実読ページ/本文範囲と採用HTTPS原本を残し、古い見解は現況を示す一次資料等と照合する。毎社/全媒体の必読・毎回取得/日次crawlerは要求しない。届かなければ一次資料へ戻る。

保有quoteはJ-Quantsを使い、公開quoteの台帳転記で補わない。補助値には時点/basisを示す。Outcomeは[Ledger](../../.agents/skills/ledger-record/SKILL.md#historical-outcome)、世界経済の解釈/発行履歴は[市場判断skill](../../.agents/skills/market-context-assessment/SKILL.md)へ。
