# 企業財務 / Company Financials

実績利益・価格・財務状態を、期間と株式basisを揃えて観測するL2。名称は[解析モデル](../architecture.md#現在の解析モデル)、旧ER/FVは[歴史評価](estimate-calibration.md#machine-valuation)を参照する。現行候補抽出は保存モデルを読み、機械ER/FVを生成しない。算式・field・欠測分岐は[所有実装](../../engine/src/baibai_engine/machine_analysis/models/company_financials/)が正本である。

EDINETは同じmarket sealで文書・訂正・抽出revisionが適格な場合の任意入力。未取得を埋めず全社生成の前提にしない。価格欠測でも財務金額・出典を残し、価格依存値だけを未評価にする。旧価格や0で補わない。

## 利益価格の観測

Yは報告年間利益÷普通株時価総額で、分配可能利益や期待収益率ではない。連結は親会社帰属利益、単体は当期純利益を使い、優先配当等の普通株帰属調整は推定しない。種類株式のある企業では会社EPSとの分子の違いを原典で確認する。

利益は円総額で合成し、自己株控除後の同じ分母で最後に1株換算する。期ごとに平均株数が異なるEPSを足し引きせず、丸め・basis不明のEPS×株数から総額を復元しない。FinancialSnapshotのEPSは会社の期中平均株数EPSとは別である。

## 会社予想と実績期間

[開示owner](../../engine/src/baibai_engine/machine_analysis/observations/financials/financial_documents.py)が値の有無より先に適用期と連結/単体basisを選ぶ。会社予想EPSによるForward PERと外部consensusを分け、利益・配当の出典を辿る。新しい期の欠測を旧期で埋めず、過年度訂正で現在区分を巻き戻さず、連結予想へ単体補足を混ぜない。

文書番号・投入順・未知の同日時刻で訂正順を作らず、矛盾はfieldごとに扱う。空欄を撤回とせず、矛盾のない他項目は残す。予想純利益と経常利益の大小や赤字注記だけで、特別損益の原因・持続性や構造的衰退を認定しない。

TTMは最新実績・前年FY・前年同期間を固定して必要fieldを確認する。配当/予想だけの文書で実績を消さず、訂正の欠測を旧revision・別期間で補わず、単一四半期を年率化しない。[期間・会計基準の境界](company-growth.md#実績を読むときの境界)は企業成長と共通だが、利益合成の最新公表額と、企業成長の保守的な累計訂正制約は別契約である。前年累計の配信が通期より後でも、対応資料の先行公表があり得るため、配信順だけで誤額としない。

[EDINET同一文書](company-growth.md#edinet同一文書の比較)・[PDF組替](company-growth.md#pdf組替比較の選択)も共通ownerへ従い、未取得だけで独立した既存経路を落とさない。会計方針等のflagは原文字列/identityを残す。L3要約の空一覧や修正再表示「無」は比較可能性の証明ではなく、flagだけで組替・除外しない。基準未確定でも原額・独立した価格/株数/配当は残し、異基準の旧FYへ候補政策を迂回させない。

## 資本と株式単位

時価総額は同じ資本状態の発行済株数から自己株を控除する。平均株数を発行済総数、非支配持分込み純資産を普通株自己資本としない。TA×EqARは同一行を使い、独立carry値を掛けない。PBRも普通株basisを照合する。新しい資本状態に自己株数等がなければ、過去の0や古い状態で整合を作らず、取得・処分・消却を推測しない。

期末株数と遡及分割済みEPS/BPSは単位が異なり得る。[補正owner](../../engine/src/baibai_engine/machine_analysis/models/company_financials/metrics/capital.py)の独立株数・factor・利益/EPS・自己資本/BPSの裏付けが揃う境界だけを補正し、根拠と前後を残す。数値一致だけで誤転記・二重控除とせず、未解決の時価総額/派生値を推定しない。

取得済みJPX資本行動PDFの完了日・普通株総数を確認できた転換は受領時計/hashを付ける。期末後の新発行済数から旧自己株数を引かず、同時点数不明なら依存値を未確定にする。未対応は完了扱いせず、有限原本群を全社網羅としない。

大量保有報告の株数は第三者の申告で、発行会社確認や変更日確定ではない。提出日・報告基準日・hashを残し、時価総額/順位を自動補正しない。受領はUTC日だけのため、同日利用は評価前の固定snapshotに存在する場合に限り、固定時刻を受領時刻の上限とする。それ以外は翌UTC日以降に扱い、時刻や網羅性を推定しない。

## EDINET由来指標

[抽出owner](../../engine/src/baibai_engine/machine_analysis/observations/financials/edinet_metrics/csv.py)がtype=5 CSVの当期・基準・連結scope・context・JPYを選ぶ。ASCIIハイフンの明示0と空欄/似た横棒、本表とセグメント次元を区別する。連結宣言に対応値がなければ単体へ戻さず、JP/IFRSを合算せず、独自concept・類似名・一部科目から全額を推定しない。

| 観測 | 評価へ読み替えない境界 |
| --- | --- |
| cash/debt・投資有価証券 | 現預金/現金同等物と確認した金融債務で、全債務の網羅保証ではない。証券は帳簿価額で含み損益・税・拘束・換金性・必要保有は未反映。asset-backed ratioは非金融のgross調査proxyで清算/公正価値ではなく、金融業へ同じ解釈を適用しない |
| EBITDA | 同書類の営業利益と対応償却。異なるCF加算子を同義として落とさず、のれん償却だけで未知の減価償却を補わない。負EVと負EBITDAを区別し、負倍率を「低いほど安い」としない |
| OCF/capex | 同context/scopeで合算/内訳を二重計上せず、支出符号は一度だけ正規化。J-Quants OCFと混ぜてFCFを作らず、内訳0の感応度を正常FCF・維持投資としない |

短信との規模照合は範囲ずれ検知で、同時点/連結範囲の証明ではない。不適格は該当EDINET値へ局所化し、他sourceや銘柄全体を消さず、basis・不足理由から原典へ戻る。

[報告フロー](../../engine/src/baibai_engine/machine_analysis/models/company_financials/reported_flows.py)の年次exactは年度metadataの品質で、最新TTM・実測context期間・経済的持続性の保証ではない。半期・変則年度・期間不明を年率化せず、倍率不適格でも報告額を残す。net cashは時点残高である。

既存EDINET metric storeにないcontext日付を年度/提出日で補わない。実期間を持つ短信OCF TTMとの精度差を残し、異期間のFCFとCFOを差し引かない。L3のBS測定日・debt scopeも開示日・期末・carry lagから推定せず、原典の時点/連結/単位/内訳を確認する。保存済み世代・判断・再開payloadは書き換えない。

## 配当と還元

予想と実績を期・basisごとに分け、L3の将来分配は原典と仮定で評価する。旧機械carryを実現配当へ読み替えない。

正の実績DPSは分割換算済み/原単位が混在し得る。期間不明・逆転・未来終了や単位変更/未分類actionを未解決とし、四半期末を基準日、配当総額÷期末株数を換算値にしない。factor相殺でも変更を消さない。明示0・無配・未観測・basis未解決を分け、使える予想と原明細/総額/株数は残す。未解決年度を飛ばして増配率/streakを復活させない。

## 価格と株数のbasis

raw closeと価格日より後のfactorでas-of basisへ調整し、権利落ち日当日に二重適用しない。異なる取得世代のprovider調整価格を接続せず、factor0をイベントなしとしない。

価格factorはrights issueも含むが、株数・1株値・配当の換算はExRTで確定した分割/併合だけ。未分類factorを分割とせず、価格調整から株数を逆算しない。price-onlyと配当込みtotal returnは[較正](estimate-calibration.md)で区別する。
