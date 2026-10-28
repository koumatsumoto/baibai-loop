# 企業財務 / Company Financials

名称の区別は[用語集](../glossary.md)。以下の旧module名と保存契約は、モジュール構成再設計の移行が済むまで現状を示す。

`machine_analysis/company_valuation/metrics`は財務・会計期間・株式basisを揃えて機械指標を作る。新しい五モデル生成は`machine_analysis/company_models.py`で実績利益・価格と財務観測を保存する。同じmarket sealに適格なEDINET metricsがあれば、既存の文書・訂正・抽出revisionの適格性を保って財務観測へ任意入力として渡す。未取得を補完せず、取得・抽出の成功を全社候補の前提にはしない。候補抽出は保存モデルを読むだけで、機械ER/FVを生成しない。算式・field・欠損分岐の正本は実装で、本書は同じ値に見える異なる経済的意味を区別する。

価格がない場合も財務観測・EDINET金額と出典を保持し、価格・時価総額に依存する倍率や利回りだけを欠測にする。過去の正価格や0で補完しない。

## 利益価格の観測

Yは報告された年間利益総額を現在の普通株時価総額で割る座標であり、普通株主へ分配可能な利益や期待収益率ではない。連結では親会社帰属利益、単体では当期純利益を使い、優先配当・優先株償還等の普通株帰属調整を推定で行わない。保存モデルはこのbasisを明示する。種類株式がある企業では、会社EPSの分子と報告純利益が異なり得るため、L3は原典の1株当たり情報・優先資本を確認する。EPS×平均株数によって丸め・株式basis不明の利益総額を作り直さない。

<a id="machine-valuation"></a>
## 保存済みの旧機械ER/FV

旧runの機械E[r]は倍率収束とcarryによるsecondary prior。以下は保存済み結果と旧較正を読むための意味であり、新しい候補抽出・L3入力には含めない。AI調査の優先度や企業固有の価値を所有しない。年率ratio、成分、入力basis、anchor・係数・clipの前提と一緒に読む。機械の5年priorは銘柄評価の期間を固定しない。

carryの配当・グロス株数変化が欠測なら計算上の寄与は0になるが、無配・株数不変を観測した意味ではない。元のnullable入力と欠測仮定を示す。予想DPS spike guardは一回性の分配を反復carryへ入れる影響を限定するため採用したが、当時の長期比較は盲検の閾値検証ではなく、5y top5 trap悪化の受容を含む。[採用時の制約](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-30-dividend-carry-spike-guard/report.md)は将来成績の保証ではない。株数減少は消却・発行等を含む過去の変化で、自己株取得の実行cash・未消化枠・将来還元ではない。

業種中央値anchorと自己レンジanchorは機械比較値で、企業の期間内分配・将来終価・現在価値とは別である。Stock Assessmentへ重複転記せず、保存run・時点とともに引用する。保存fieldの不在、明示null、0を混ぜず、旧snapshotへ後付けして再発行しない。

保存済み照会は選択runの値を読む。論理as-of cutoffで選ぶrunは後日生成した過去replayもあり、当時既知の保証ではない。対象runなし・run内tickerなし・計算不能を区別し、古い値や候補一覧で埋めない。企業の条件付き算術は[Stock Assessment](stock-assessment.md#scenario-arithmetic)、歴史分布は[較正](estimate-calibration.md)。

<a id="carry-policy"></a>
### 機械priorの採否

carryはグロス株数変化を維持する。自己株控除後の変化は別のfactとして残すが、当時の比較はrank ICが劣り、分位差も時間分割で反転し、置換を支持しなかった。較正の実現率引上げとupside/buyback cap拡張も両horizonの改善を確認できず採らない。これは将来の有効性を否定する判定ではない。詳細は固定した[株数比較](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-17-tradable-share-change/report.md)、[実現率](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-01-er-level-calibration/report.md)、[cap比較](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-10-er-distribution-context-v2/report.md)に残す。

## 会社予想と実績期間

機械Forward PERは会社予想EPSを使い、外部consensusとは分ける。数値の有無より先に適用する会計期間と連結/単体basisを決め、当期・翌期、利益と配当のsourceを識別する。新しい期の欠測を古い期で埋めず、過年度訂正で現在区分を巻き戻さない。連結会社の単体補足を連結予想へ混ぜない。

文書番号の大小や投入順はrevision順ではない。開示時刻不明も同日の既知時刻より古いとは決めず、順序を確定できない矛盾はfield単位で未解決にする。EPS等が一致する他項目まで落とさない。空欄は非掲載であり撤回の確認とは限らない。原典の対象期・basis・statusは保存モデルの`financial.earnings_forecast_source`と`financial.dividend_forecast_source`（旧runではStock Analysisの`metrics`内の同名field）から辿る。

予想純利益が経常利益を上回る注記は大小関係で、特別利益の発生・原因・持続性を確定しない。赤字予想注記も一時損失と構造的衰退を判別せず、単独の除外や減衰にしない。赤字のEPSや実際に使うanchorは個別入力から読む。

実績TTMは会計期間で最新実績を選び、比較する前年FY・前年同期間を確定してから必要fieldを見る。配当/予想だけの文書で実績を消さず、最新実績訂正の欠測を古いrevisionや別期間で補わない。単一四半期の単純年率化はTTMではない。連結/単体区分を揃えても連結範囲や会計方針の不変までは保証しない。

利益価格の内訳は保持したsource内の最新公表された期間別金額と開示日・時刻・文書番号を保持するが、訂正同士の経済的整合まで照合済みとはしない。前年累計の数値配信が通期より後でも、通期公表日までに訂正内容が別資料で公表されている場合があるため、配信順だけで誤額と認定したり一律に除外したりしない。L3は重要な訂正について原典の対応を確認する。企業成長の保守的な累計訂正制約と、最新公表額の算術を同じ契約と扱わない。


合成は円総額で行い、1株換算は最後に一度だけ行う。各期EPSは異なる平均株数で割られており、EPSの和差では株式数が変わった企業を誤る。FinancialSnapshotのEPSは時価総額と同じ自己株控除後分母で組み直した値で、会社開示の期中平均株数EPSとは別である。

3FY平均EPSのnormalized PERは、連続FYとsplit-safe basisを揃えた算術平均。循環や一時損益を経済的に正常化した利益ではない。旧Normalized Earningsの候補抽出で使っていたが、新しい二viewのmembershipや順位へは使わない。旧機械ER/FV anchorへも投入しない。後者は長期比較で増分効果を支持せず、[不採用](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-02-normalized-per-production/report.md)とした。必要年度の不正・欠測を古い年度で埋めない。

YoYは同じ会計期間・利益field・連結区分を比較し、直前開示とのQoQを読み替えない。比較不能のnullは改善なしではない。負の前年利益の比率符号だけで改善を読まず、赤字縮小を別に扱う。旧指標の選択利益に経常/純利益fallbackがあっても、営業利益を代替しない。新しい五モデルの成長変化はcompany_growthが所有し、負基準の旧前年比を併記しない。

## 資本と株式単位

時価総額は同じ資本状態の発行済株式数から自己株を除く。自己株を含めると現金比率・利回りが薄まり倍率が割高に出る。期中平均株数はgross issuedの代わりではなく、普通株自己資本は非支配持分を含む純資産と異なる。

TA×EqARは同じ開示行から作り、独立carryした最新値を掛けない。PBRのBPS経路も普通株basisと照合し、新しい資本状態を古い状態へ戻して整合を作らない。新しい発行済株数が自己株数を欠く場合、過去の自己株0も現在0の証明ではない。欠測だけで取得・処分・消却を推測しない。

報告利益の総額をEPS×期末株数で復元せず、発行済/平均株数の数値一致だけで誤転記や二重控除を断定しない。分割跨ぎの発行済と自己株は同じ単位へ揃える。資本basis未解決の時価総額・派生指標は推定で埋めない。

## EDINET由来指標

type=5 CSVの当期・会計基準・連結scope・context・JPYを選ぶ。CSVのASCIIハイフンは仕様上の明示ゼロで、空欄や似た横棒とは別。contextの期間と書類metadataの期間は一致するとは限らず、半期の書類期末をCF測定期間へ使わない。財務本表とセグメント等の次元を混ぜない。

連結作成宣言があるのに対応する当期値がなければ単体へ戻さない。日本基準とIFRSの併記を合算せず、検証済み標準conceptだけを対応させる。企業独自・類似名・一部の債券借入金から全金融債務や投資有価証券を推定しない。

cashは現預金/現金同等物の定義、debtは確認した金融債務の合算であり、全債務の網羅保証ではない。investment securitiesは選択scopeの帳簿価額で、含み損益・売却税・拘束・換金性・必要保有を反映しない。asset-backed ratioは非金融企業の調査入口となるgross proxyで、liquid/fair valueではない。金融業は資金と証券が事業そのものなので同じproxyを適用しない。

EBITDAは同じ書類の営業利益と対応する償却。異なるCF加算子を同義として落とさず、不明な減価償却をのれん償却だけで代用しない。EV/EBITDAが負なら「低いほど安い」という比較は成立しない。負EVの資産文脈と負EBITDAの利益文脈を区別する。

OCFとcapexは同じcontext・scopeを使い、合算と内訳を二重計上せず、取得支出の符号を一度だけ正規化する。J-Quants OCF TTMとEDINET OCF/capexは別系列で、sourceを跨いでFCFを合成しない。内訳を0にした感応度を「正常FCF」や維持投資の認定としない。

短信とEDINETの規模照合は範囲ずれを検出する検査で、通過しても同一時点・連結範囲の証明ではない。不適格は該当するEDINET値へ限り、他sourceの指標や銘柄全体を消さない。source/basisと不足理由を残して原典へ戻る。

exact/approximated/unavailableは取得フローと期間の品質。年次metadataの12か月確認と、半期等を完全TTMへ合成できることは別で、半期CFを機械年率化しない。net cashは時点残高で12か月フローではない。品質は経済的持続性・株主回収可能性・倍率全体の正しさの認定ではない。

Investment JudgmentのBS文脈で測定日やdebt scopeが未保存なら、最新開示日・書類期末・最大carry lagから推定しない。金額と品質だけでなく原典の時点・連結・単位・内訳を必要な範囲で確認する。

## 配当と還元

DPSの会社予想と実績を対象期・basisごとに分ける。旧機械priorの5年反復するcarryでは、正のsplit-safe実績に対して予想が2倍超なら、特別配当と恒常転換を識別できないため実績を使う。実績0や未観測では初配当等を消さないため予想を使える。これは予想の不可能性や持続性の判定でなく、L3の期間内分配は原典と仮定から別に評価する。

正の実績DPSは会社が分割後へ先行換算した値と原基準値が混在し得る。会計期間から比較日まで単位変更・未分類actionがあれば、四半期末を配当基準日にしたり配当総額÷期末株数で換算したりしない。正の実績の期間不明・逆転・未来終了も未解決で、明示0は株式basisに依存しない観測として保持する。

basis未解決と無配・未観測を分け、独立して使える予想まで妨げない。相殺して累積factorが1でも変更をなかったことにしない。原本の明細・総額・株数は残し、未解決年度を飛ばして増配率やstreakを復活させない。将来carryを実現配当へ読み替えない。

## 相対比較と自己レンジ

旧runの倍率業種中央値は当時のScreeningの共通資格母集団で作る。新しい五モデルの市場・同業文脈とは区別する。母数下限は少数の中央値へ依存しすぎないための慣例で、経済的最適値ではない。軸ごとの欠測が異なるので業種/市場fallbackも軸ごとに異なる。market fallbackのgapを「同業より割安」と読まず、構成の違いとbasisを併記する。

自己レンジは**現在fundamentalsを固定し、過去価格だけを動かした倍率比較**。各日の当時利益・資本から再現した倍率履歴ではない。min/max内の位置は観測順位のpercentileではなく、短い履歴の位置0も長期割安の証拠ではない。bar本数、履歴期間proxy、scope内の相対密度を合わせて読む。密度は取引calendarの完全性を証明しない。

真の倍率履歴anchorへの置換は、必要な5y確認窓が成立せず[不採用](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-17-self-range-vs-multiple-history/report.md)とした。

価格調整はraw closeとfactorからas-of basisを作り、取得世代の異なるprovider調整済み価格を接続しない。価格日より後のfactorを使い、権利落ち日当日に二重適用しない。factor0をイベントなしとせず、正の有限係数を要求する。

価格factorはrights issueも含む。株数・per-share・配当の単位換算はExRTで分割/併合と確定したものに限り、未分類factorを分割と推定しない。価格調整から新株数を逆算しない。price-only系列と現金配当込みtotal returnは[較正](estimate-calibration.md)で区別する。
