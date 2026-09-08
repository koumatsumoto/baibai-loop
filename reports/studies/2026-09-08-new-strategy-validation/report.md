# 新戦略の実判断点検と初回前向き観測

価値tier: T1 — 見送りの理由と再検討漏れを実例で区別し、現在の判断を変える確認事項へ調査を集中する。

本番の選定・売買条件は維持する。原4社の再計算は購入を要求する誤りを示さず、利益の持続性、資産の使途、倍率に評価の分岐が残った。一方、現在の3539には直近Thesis後の重要な下方修正があり、個別のPosition Reviewへ戻す必要がある。新規caseの調査と将来観測はこの後続作業や将来の成績成熟を待たずに開始した。

## S0: 固定した範囲と観測限界

exact ID・原価格・選定前提は[事前計画](plan.md)に固定した。基点はmain `317ca38c413c89872c36f8d7229eabd48a7f9051`、最初の計画commitは`b826cc6d`。原判断の構造を調べた主担当は原結論を知っているため、独立担当には原disposition/CAA/倍率を渡さず、一次資料18件から先に仮説・反対仮説・探索価値レンジを作らせた。初回メモは2026-09-08 20:10:18 JSTに固定し、その後にだけ原文を照合した。後知恵を完全に遮断した予測試験ではない。

local application/schema20、runs/5、market/25が読め、原4件とReview/CAA、canonical run全3,705 Analysisを保存できた。cloud最新性は検証していない。原資料の欠損による全case不能は0件。独立担当がオンライン再取得できなかったサクサQ1とセラク買戻しは、主がURLから再取得し保存PDFとbyte一致を確認した。一次資料の全文全項目を監査したという意味ではない。

既存較正は`price_return_only`、48月次cohort（2022-09〜2026-08）、snapshot contract `6e55900dfcb6508c`。rules hash `37aa2c1014642287`は今回productionの`c525a6c55309450f`と違う。JPXを含むCandidate Discovery fidelityは44時点でunavailable、完全なのは2026-05〜08の4時点だけ。9月7日までの暦上の成熟は3m=45、6m=42、1y=36、3y=12、5y=0時点であり、3y成熟の時点に必要なfidelityが揃うわけではない。forward returnの成績を読んで良いcaseを選んでいない。過去JPXの欠落と5y未成熟を全期間rebuildで解消できるとは扱わず、現行手法の収益優位をこのsnapshotから認定しない。

## S1: 原4社の経済レビュー

全4社について一次資料から具体的な価値仮説と判断を変える質問を作れた。独立Research価値は全件Yだが、購入判定とは異なる。下記のBase/Downsideは確率・信頼区間ではなく、作者の条件付き見積りである。

### 6675 サクサ: 売却後の資金を本業利益と分ける

主因は企業評価のdefer（実質余剰資金と継続利益の評価未完了）。原horizon・要求年率・Base/Downsideはnullであり、原評価を12か月・10%に補完してPmaxを作らない。

[Q1短信](https://pdf.irpocket.com/C6675/xoA3/ieAo/GLWS/R9vw.pdf)の土地売却益231.77億円、通期NI165億円を反復利益PERへ投入することはできない。本業の会社営業計画は10億円、Q1は2.86億円。Q1末basisは4月の1:3分割後、発行18,734,886−自己1,311,096=17,423,790株。ただし[7月24日処分完了](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260724/20260723598532.pdf)の67,800株を加えた9月7日既知分母は17,491,590株。S1初回と原Thesisのdilution説明はQ末値のままだったことを後続深掘りで検出した。差0.389%で原h/FV null・deferを覆さないが、[正式記録の訂正 #1267](https://github.com/koumatsumoto/baibai-loop/issues/1267)へ分離する。処分総額を無条件の現金増へ加えない。[構造改善開示](https://pdf.irpocket.com/C6675/xoA3/tx0r/Pwbb/jNwR.pdf)では138人が応募し、追加費用は精査中。現預金192.33＋短期証券130−税84.17−借入14.48=223.68億円は、運転資金・構造費・投資控除前の計算であり全額excess cashではない。

独立担当は構造改善引当12.69億円も将来支払として控除し210.99億円を出発点にした。既に損益へ計上した費用を将来利益から再度引くことと、未払い引当のcash流出を評価することは別である。[資金計画](https://pdf.irpocket.com/C6675/xoA3/SFxs/Zm8J/KluW.pdf)には成長・不動産・M&A・基盤投資と還元があり、cashを無傷の下支えにはできない。投資支出をすべて永久損失とせず、取得する事業価値も対応させる。

独立探索は正常EBIT10〜25億、8〜10倍、経済的拘束後金融資産150〜200億で約1,320〜2,583円（S1初回のQ末分母。既知株数訂正後は約1,315〜2,573円）。これは正式FVではなく、原価格2,177円の結論が正常利益と資金使途で逆転することを示す感度である。EBIT10億・8倍・金融資産180億なら約1,492円で、単なる低PBRは購入根拠にならない。[予想配当125円](https://pdf.irpocket.com/C6675/ccdN/qub4/Fbl3.pdf)を資産価値へ別に足す場合、その原資を残余金融資産から減らす必要がある。将来EBITが幅を持つこと自体をunknownにせず、追加構造費・投資時期・留保資金が価値をどれだけ動かすかへ問いを絞る。

再編が遅れる経済シナリオは、固定FVを1年延ばすのでなく、人件費削減の遅れ・追加費用・投資先行による残余cashと正常EBITの同時減少。再検討は追加退職費用の確定、半期決算と資金使途の具体化。原入力の算術訂正だけでbuyへ変わることは確認できなかった。

### 5946 長府製作所: 巨額金融資産は現金でも即時分配でもない

主因は企業評価defer（資産の使途・損失幅の評価不足）。原文は12か月以内の換金・還元確約を必要条件にしておらず、この誤読は採用しない。原horizon・要求年率はnullを維持する。

[H1短信](https://www.chofu.co.jp/user_data/news/1786084414.pdf)の金融資産は現金55.96＋短期証券93.83＋投資証券915.76=1,065.55億円。本業営業利益5.61億円に対し匿名組合損8.05億円があり、金融運用益を含むNIへPERを掛けて全金融資産を加えると二重算入になる。34,239,312−237,854=34,001,458株。金融資産から全負債123.50億円を控除した単純比は2,770.62円/株だが、清算床ではない。金融資産を20%減額すると2,143.85円、30%なら1,830.47円となり、原価格2,124円の余裕は資産調整に敏感である。

[有報](https://www.chofu.co.jp/user_data/news/1773808149.pdf)の社債には長期償還が多く、帳簿資産を12か月の現金と同一視しない。短期借入30百万円だけを総有利子負債とせず、H1の長期借入流入107百万円なども橋渡しする。これは今回の感度に比べ小さいが、精密なEVへ進む際には確定が要る。借換不能と推定したわけではない。

独立担当は資産拘束・信用・税・運転留保と本業EBIT18〜24億の組合せで約2,073〜2,900円の探索幅を作った。仮置きhaircutの端を正解にせず、[中期計画](https://www.chofu.co.jp/user_data/news/1771556136.pdf)の投資・還元方針と匿名組合の追加義務を調べ、どの資産がどの期間に株主の見返りへ届くかを問う。価格が動かず46円分配を仮定する例ではcarryは約2.17%に留まり、資産額だけから高い年率は出ない。6月末権利の既配当は新規購入者の将来分配に混ぜない。

経済的遅延では長期債の利息と金利/信用損、再投資の収益、分配後資産を同時に動かす。確実な触媒が無いだけでNにせず、資産アクセスの調査価値はY。原入力訂正だけでcandidate/buyへ変わるとは認定せず、次の財務開示で匿名組合損・資金使途を再確認する。

### 6430 ダイコク電機: 企業評価と原価格での不足を分ける

主因は価格。原企業評価はcandidateで、NI31億円、14,617,492株、PER11倍、将来配当100円、12か月・要求10%の算術は再計算と一致した。[Q1短信](https://www.daikoku.co.jp/ir/wp-content/uploads/2026/08/260807_tansin.pdf)の会社予想と[有報](https://www.daikoku.co.jp/ir/wp-content/uploads/2026/06/53rd_yuho.pdf)を突合し、NI×PERに現金を足したり負債を再控除したりしていない。会社EPS212.91と再計算EPS212.07の違いは株数時点差で、利益計算の誤りとしない。

| 原12か月算術 | Base | Downside |
| --- | ---: | ---: |
| NI / PER | 31億円 / 11 | 15億円 / 9 |
| terminal 円/株 | 2,332.82 | 923.55 |
| cash 円/株 | 100 | 60 |
| 原価格2,469円からの年率 | −1.47% | −60.16% |

Pmaxは2,211.66円。原価格で要求される総価値2,715.90円から分配100円を除くterminalは2,615.90円で、PER11固定ならNI34.76億円、NI31億円固定ならPER12.33倍が必要。これはPmax不足を別の単位で表したもので、二つの独立した悪材料ではない。

独立担当のNI25〜35億、PER9〜11倍の探索幅は1,540〜2,634円で、31億・11倍が明白な算術誤りとはいえない。ただし[説明資料](https://www.daikoku.co.jp/ir/wp-content/uploads/2026/08/260807-presen.pdf)のサービス売上成長とスマート機普及後の利益持続性、ホテル投資などのcash使途が分岐点になる。全社を高倍率サブスクとみなさず、[会社Q&A](https://www.daikoku.co.jp/ir/wp-content/uploads/2026/05/260527-qa.pdf)と整合させる。

固定terminal/cashのh+12年率−0.74%は純粋な時間感度。別の経済的遅延例として、機器需要先送りと開発/投資費先行でNI25億円、PER11、株数同じ、2年間100円配当を維持するとterminal1,881.31＋cash200円、24か月年率−8.19%。これは会社予想でも新しい売買gateでもなく、利益減少を時間だけで隠さないための条件付き反証である。既存入力の訂正だけで価格判定は変わらない。再検討は有効評価のPmaxへの接近、または決算で正常利益/資本使用が変わる時。

### 6199 セラク: Pmax内でも配分を自動化しない

主因は資本配分での見送り。原企業評価はcandidate、Pmax1,362.63円に対し原価格1,320円は内側だが、CAAはPM/営業制約と薄い条件余裕を理由にdeferした。これは人間の未裁定や操作停止を主因とする記録ではない。

[Q3短信](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260715/20260715594349.pdf)のNI12.48億円、会社FY予想18.7億円に対し、原Base17億円は[前期有報](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100X5OA.pdf)の17.10億円近傍。Q4に必要なNIは6.22億円であり、計画達成は自明でない。[説明資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260715/20260715594357.pdf)のPM/営業制約と高付加価値案件比率を検証し、AIの将来利益を既存利益へ加えていない。

| 原12か月算術 | Base | Downside |
| --- | ---: | ---: |
| NI / PER | 17億円 / 12 | 12億円 / 9 |
| terminal 円/株 | 1,498.90 | 793.53 |
| cash 円/株 | 0 | 0 |
| 原価格1,320円からの年率 | 13.55% | −39.88% |

原分母はQ3自己株控除後13,183,495＋旧予約権355,700＋[新予約権61,300](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260731/20260731505688.pdf)を13,610,000へ切上げ。[Q3後買戻し](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260901/20260901529440.pdf)を控除しない保守的仮定であり、[自己株消却](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260806/20260806512092.pdf)による二重の株数減少はない。独立担当の初回13.1百万株は旧予約権の未照合を含む探索値なので、原値の訂正として採用しない。8月末権利17.4円は新規購入者の将来分配へ加えず、次年度未公表分は原仮定0のままとした。実績分配不明を0にしたわけではない。

原要求総価値1,452円に対する余裕46.90円は3.55%相当。PER12なら必要NI16.47億円、NI17億円ならPER11.62倍が必要。独立探索のPER9〜11では原12倍への異論が残るが、どちらも事実ではない。既存DXの継続利益を支える単価・PM稼働の証拠が判断を変える。経済的遅延例はPM費が先行してNI14億円、PER12、同じ希薄化株数、cash0、24か月terminal1,234.39円で年率−3.30%。固定価値h+12の+6.56%とは別である。

Downsideだけを損失上限gateへ昇格せず、全保有とcashとの比較で配分を決める。原入力訂正だけのbuy転換は確認できなかった。次のFY決算とPM/高付加価値案件の反復利益は既存`task-20260908-6199`（2026-10-16）へ接続する。

## 現在の再検討待ちと保有の軽い点検

現行schema v4のReviewed Thesisは上記4社だけで、candidateは6430/6199。有効評価を旧revisionへ戻していない。watchを9月8日で実行すると当日raw barがstoreになく`missing_raw_bar:2026-09-08`となるため、現在価格での購入条件充足は2社とも未評価。9月7日価格のPmax診断を9月8日の取引可能価格と呼ばない。

報告済みledgerと最新Position Review/taskを照合した。旧schemaの保有判断は現在の残存見返り評価へ自動変換できず、全保有を一斉再評価していない。取得済み財務metadataの直近Thesis後の更新を調べ、以下の実例だけ一次資料で追加確認した。全社IRの網羅監査ではなく、未報告broker状態も推定していない。

| 対象 | 観測 | 影響と次のtrigger |
| --- | --- | --- |
| 3539 | [9月3日修正](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260903/20260901530172.pdf): OP109→90億円、NI70→49億円、店舗減損10.42億円。価格転嫁遅れによる粗利低下が理由。直近Thesisは8月14日、対応するopen taskなし | 価格下落ではなく本業利益と回収可能性の変化。正式Position Reviewで投資理由・残存見返りを再評価する。9月11日決算を追加資料に使うが、今判明したeventを無かったことにしない |
| 9692 | [8月21日修正](https://www.cec-ltd.co.jp/ir/2026/08/accounting_20260821.pdf): 官公庁案件によるNI56→59億円。直近Thesis後のevent | 9月10日の既存`task-20260910-9692-q2-review`あり。重複taskを作らず同決算で持続利益・残存見返りを確認 |
| 9715 | 8月5日付の財務更新は7月31日Q1の期中レビュー報告添付に対応 | 新決算やmaterial changeと機械的に判定しない。[IRライブラリ](https://www.trans-cosmos.co.jp/ir/library/account.html)で区別 |

受取済み分配を将来価値に加える、買付floor不足や価格下落だけでexitにする実例は、この限定点検では確認されなかった。旧schema保有の現在の権利basis・残存見返りを全件verifiedとするものではない。

## S1の統合と#1249の判定基準

| 分類 | 対象と結論 |
| --- | --- |
| confirmed_error | 原4社の結論を変える重要な事実/算術誤りは0件。ただし6675のQ末分母に完了済み自己株処分が未反映だった事実記載1件を後続深掘りで検出し、#1267へ分離。独立メモ自身の長府「短期借入30だけの小計」を総額と誤読し得る表現は訂正した。原判断の欠陥や本番修正Issueとして数えない |
| supported_no_change | 6675の株数記載訂正を除く原計算basisと判断段階、原CAAの新規配分なしを撤回する証拠は無い。特別利益・権利済配当・買戻し/消却は原判断が既に区別していた |
| judgment_disagreement | 6199のPER12は現在の同NI換算10.57倍より上昇を要する。10倍なら要求を満たすNIは19.76億円。6675/5946の重要unknownと評価幅、6430/6199の遅延時の利益持続にも異論が残る |
| unverifiable | 6199の原過去PER12.4〜13の同時点basis、exact fully diluted株数、6675追加費用時系列、5946匿名組合の追加義務、6430ホテル取得条件など。全caseの資料欠損とは分け、これらを検証済みへ昇格しない |

分類は排他的な会社勝敗ラベルではない。同じ会社の算術を支持しつつ仮定に異論があり得る。S1の4社を将来の上昇/下落で正誤判定することも、Research価値Yを購入件数として数えることもしない。

2026-09-08 20:21:28 JSTに#1249のrubricを固定した。Yは一次根拠から具体的な価値仮説と判断を変える問いが作れること、Nは関連事実を十分確認して経路を否定できること、Uはその研究価値を判定できない重要欠損・時点矛盾・根拠ある不一致。赤字・無配・割高・将来利益の幅・FV未完成・12か月内の還元確約なしだけでN/Uにしない。今回4社の答えは後続blind判定者へ渡していない。

PR #1262の説明は、6199の「過去比で控えめな倍率」と「現在から倍率回復不要」の違い、6430の利益34.76億円という必要条件を明確にする助けになった。一方Pmaxと必要総価値は同じ式であり、それを表示しただけの部分は説明の繰返しである。時間固定感度から独立した経済的遅延の問いを得たが、収益改善・時間短縮・#1262単独の因果効果は確認していない。

## S2: 新規4社を固定し、独立Reviewと比較を完了

S1後に最新Triageのpriority先頭から、保有/予約と既存v4を除外して6419/4231/2415/2221を固定した（計画commit `eed52c6d`）。全4社に旧v3はあるが、新戦略v4の検証は初回。正式Researchのadmissionを変更せず、active Operationも完了させていない。資料cutoffは9月7日JST日末、価格は同日15:30の未調整close。主の初回は20:33:20 JST、原判断未開示の独立contextは一次資料20本から独自の利益・資産・倍率・反対仮説を作り、固定後にのみ照合した。

企業candidate 3、企業defer 1。数値化した3社は12か月・要求10%。主の統合は全件配分見送りだが、3社の評価を企業rejectへ変更したわけではない。正式判断のpublish、人間裁定、約定はいずれも未実施。

| ticker / 原価格 | 主Base: NI億円×PER / 株数百万 | terminal / cash 円 | Base年率 / Pmax円 | Down: NI億円×PER、cash円 / 年率 | 統合判断の主因 |
| --- | --- | --- | --- | --- | --- |
| 6419 / 3,190 | 55×10 / 17.394285 | 3,161.96 / 150 | 3.82% / 3,010.87 | 30×8、100 / −53.61% | 収益法では価格不足。資産別法の大きな上値余地も残り、企業の価値不存在とはしない |
| 4231 / 1,051 | 22×10 / 19.5 | 1,128.21 / 38 | 10.96% / 1,060.19 | 12×8、24 / −50.87% | 正常利益と倍率の小変化に対し条件余裕が薄い、個別の資本配分見送り |
| 2415 / 1,686 | 22×9 / 10.377962 | 1,907.89 / 71 | 17.37% / 1,798.99 | 14×7、40 / −41.62% | 比較調査の第一候補。9倍の価値根拠または8倍でも必要利益を持続する証拠を支持し切れない |
| 2221 / 3,065 | null | null | null | null | 旺旺資産と大型投資の経済帰属・時系列が評価を変えるため企業defer。12か月換金の確約不足だけを理由にしない |

これらは条件付き予測であり、Downを永久損失下限や新たな許容下落率へ昇格しない。NI倍率法には金融収益が含まれ、cash/負債を再加減していない。将来配当は予測と明示し、後日の実績へ代用しない。

**6419 マースGHD。** [Q1](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260730/20260723598585.pdf)のNI12.66億円は35%減、会社FY67億円に対し主は特需一巡を含む55億円を置いた。[有報](https://www.mars-ghd.co.jp/ir_download/6419_2603yhyh.pdf)の過去正常利益・顧客集中と、[9月1日買戻し](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260901/20260901529909.pdf)、[8月27日自己株処分](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260827/20260821524514.pdf)を橋渡しし、Q末から7–8月取得554,200株だけを控除、実処分17,300株を加算した。独立56億×9の末価2,897.50円も要求未達。一方、金融資産・運転留保・税と金融収益を除いた本業を分ける独立探索では約4,119円も可能であり、収益法だけを企業価値の唯一の答えとしない。資産の認識・用途・本業持続利益が重要な未確定仮定である。原価格10%には主10倍でNI58.43億円が必要。経済遅延では24か月NI45億×10、分配250円、年率−5.69%。これは単なる期間延長ではなく機器需要と利益の低下を伴うstress。

**4231 タイガースポリマー。** [Q1](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260810/20260807514125.pdf)と[有報PDF p20/87](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100YK4M.pdf)ではHonda41.5%、Kuriyama12.6%の顧客集中。国内転嫁改善と米州減益が相殺し、NI22億×10と独立24億×9は別の経済仮定である。[説明資料](https://tigers.jp/ir/pdf/0803-e14.pdf)のFCF22.93億円には定期預金純払戻29.20億円が入り、営業CF24.164−有無形capex35.779=−11.615億円と分けた。[6月18日訂正](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260618/20260617573011.pdf)は明細組替でCF総額不変。期末自己株に信託を含むため再控除せず、[7月買戻し](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260716/20260716594913.pdf)後実績19.287366百万株に対し19.5百万は将来交付を含む主の保守的仮定。独立利益24億×9を同分母に揃えると9.01%で要求未達。原価格10%には主10倍でNI21.80億円、経済遅延24か月NI18億×9・分配62円で−7.83%。感度が悪いこと自体を一律の購入禁止にせず、顧客・CF・余裕の組合せで配分を見送った。

**2415 ヒューマンHD。** [Q1](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260807/20260806513158.pdf)は営業利益5.9%減に対しNI31.2%増で、補助金2.50億円を4倍して恒久利益にしない。[説明資料p12](https://ssl4.eir-parts.net/doc/2415/ir_material_for_fiscal_ym3/209968/00.pdf)の契約負債総額86.79億円（教育内訳83.22億）に対応する留保と給与支払を考え、現金301.76−借入107.23億円を全額余剰としない。[有報](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100YIDK.pdf)の年営業CF32.83−有無形capex10.05=22.78億円は正だが、前受季節性と人件費を除く反復性を問う。主22億×9で17.37%、独立23.5億×8で11.66%、同じ22億×8なら4.80%。原価格10%には8倍でNI23.14億円、22億なら8.4137倍が必要。9倍は不可能な値ではないが、現在の同NI換算7.95倍からの再評価を含む。[予想71円](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260515/20260515536160.pdf)だけを将来分配とし、過去の記念配当を足さない。経済遅延24か月NI18億×8・分配111円では−5.72%。独立異論を多数決で解消せず、補助金を除く利益と前受のcash転換を次の問いにした。

**2221 岩塚製菓。** [Q1](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260810/20260810516528.pdf)は営業赤字0.40億円、投資有価証券503.49億円。[有報](https://www.iwatsukaseika.co.jp/wordpress/wp/wp-content/uploads/2026/06/73_securities-report.pdf)の旺旺株608,434,480株の保有価値と受取配当をNI倍率へ二重算入しない。6年200億円超の投資計画は株主還元との競合だが、支出全額を価値破壊とも扱わない。[8月19日取得](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260819/20260819522784.pdf)は25万株とcash7.65億円の双方へ反映し、実績10.008687百万株とBBT交付感度を区別した。静的資産認識70–80%で約2,835–3,205円、独立別法は分配前総価値約3,042円だが、税・時価・投資時系列に敏感な探索値。独立初回でこの静態価値へ配当32円を足したbasis不足は照合時に撤回し、原horizon/要求/returnはnullを維持した。

独立Reviewで主の顧客引用頁、契約負債の名称、4231の訂正source参照を補正したが、3社の算術と4社の株数bridgeは一致した。独立初回の固定terminalによる待機感度も経済的遅延とは区別し直した。これらは検証用draft内で解消した誤りで、canonical判断のバグとして数えない。不要な人間照会・既知入力の再質問は0件。人間の通常最終裁定を省けることの証拠ではない。O4の一次資料・調査は#1249と共用し、正式Thesisを二重publishしていない。比較用の追加候補がこの4社を上回るかは#1249の独立した枠比較へ渡す。

## 前向き観測の原本と登録したtask

原予測は2026-09-08 **20:51:36.613526 JST**にprivate `s2-original-forecasts.json`として固定した。SHA-256は`74acc0313ce178d4095308860caff64ad8e4a09f69f5089b90296820bfba2091`。Base/Down・株数・cash・原価格・horizon・要求年率・企業評価・提案可否・根拠・次eventを持ち、後日revisionで原予測を置き換えない。初回4社の再分析はこのprospective cohortに混ぜない。

診断entryはdecisionより厳密に後の最初のJPX営業日close、9月9日。今回は未来で`not_observed`。原価格から遡って買えたとは仮定しない。[JPX休業日](https://www.jpx.co.jp/corporate/about-jpx/calendar/index.html)と取得済calendarを照合し、3m=12月8日、6m=2027年3月8日、原12m=9月8日は営業日。非営業日の場合は対象日以前の直近営業日へ寄せる。2221は原horizon nullなので12m満期を作らない。

| 登録task | 期限 / 問い |
| --- | --- |
| `task-20260908-follow-up` | 2026-10-08、または通常運用の最初の12新規tickerの早い方。運用負担・不能理由・具体誤りを点検 |
| `task-20260908-follow-up-2` | 2026-12-08、全4社の3m中間観測 |
| `task-20260908-follow-up-3` | 2027-03-08、全4社の6m中間観測 |
| `task-20260908-follow-up-4` | 2027-09-08、6419/4231/2415の原12m照合 |
| `task-20260908-3539` | 2026-09-09、公表済修正の個別Position Review。後続[#1264](https://github.com/koumatsumoto/baibai-loop/issues/1264) |

問い・必要一次資料・正本とprivate保存場所・確認後の判断をtask本文へ記録した。登録直前に同日同問を確認し、新規5件のみ登録。非task全canonical tableの行数/内容hashは登録前後不変、着手前からの既存taskは更新しなかった。反証Review後、今回追加したcohort3taskだけ本文を訂正し、2221も3m/6mの事業・価格・確定分配・1306/cash比較へ含めた。原horizon/FVの満期・予測誤差は補完しない。訂正前後も非task内容不変を確認した。R2/publish/dispatchは行っていない。#973の既存3task（9月27/28日）、#1010の既存task（9月29日/10月17日等）の存在・期限をread-only確認し、重複追加しなかった。

評価対象は三つに分ける。実資産全体は報告済ledgerと`position/outcome.py`のcash・予約・全保有込みTWRと円損益・external flow。予測診断は全caseの事業仮定と原1株相当の価格・確定分配。Discoveryは#1249と既存calibration。実portfolioには旧戦略・人間裁定・約定差が混じり、新戦略単独の因果効果とは呼ばない。

比較は同entry/endの**1306 ETF proxy**と金利0の診断cashに固定。price-onlyを先に別名で出し、total-returnは権利発生済の確定分配を原株basisへ換算して非再投資で加算、支払済/未払を分ける。未知の分配・企業行動は0にしない。税費用前の診断と実TWRを同列比較しない。公式TOPIXの任意期間が既存ownerで厳密に表せない場合はその欄だけ比較不能とし、3m/6mを1yへ偽装しない。小標本、ticker反復、同相場の相関、保有/見送り選択バイアスがあり、勝率・平均return・Sharpeだけでalphaを宣言しない。

## S3: 維持・引継ぎと完了範囲

| 検証点 | 対象 / 不能 | 観測と原判断への影響 | 選択 / owner / 次trigger |
| --- | --- | --- | --- |
| 原4社 | 4 / 全件資料欠損0、部分unknownあり | 株数記載訂正1（6675、#1267）、結論を変える誤り0。原no_allocationを覆す根拠なし | 維持＋記載訂正 / Research・CAA / #1267と個別の利益・資産用途・倍率根拠の変化 |
| 有効candidateの現価格 | 2 / 2 | 9月8日raw barなし。購入条件内の取りこぼしは未評価 | 未実施範囲を[#1266](https://github.com/koumatsumoto/baibai-loop/issues/1266)へ移管 / 既存watch / 通常価格取得後に2社を点検 |
| 現在の保有軽点検 | 取得済metadataと全既存判断/task、3eventを一次確認 | 3539の下方修正を個別に確認。他保有の全法務/残存価値は未監査 | 最小追加調査 / Position Review / #1264・登録task |
| #1249 rubric | 原4 / 0 | 研究価値と購入可否、未来幅と重要unknownを区別 | 維持 / #1249 / 固定標本の比較 |
| 新規draft | 4 / resolved3、企業評価unresolved1 | 独立異論で評価倍率の重要性を明確化。正式配分は未実施 | 原予測保存 / Research検証 / 登録cohort task |
| 長期較正 | 48時点 / 44でDiscovery fidelity不足 | 現行手法の3y/5y優位の裏付けに使えない | 範囲を限定 / calibration / 具体的手法変更が必要な時に欠損と契約を再評価 |

次の1作業は **#1264の3539個別Position Review**。公表済み利益修正を原投資理由へ照合し、根拠あるhold/exit/nullまたは重要資料不足を通常手順で確定する。取得不能をPASSへ置換せず、正式判断と取引は本検証の外に置く。新戦略の予測成績と#1262の収益因果効果は未検証だが、将来成熟待ちは登録taskへ引き継いだため今回の提出を止めない。

## 再読・引渡し・検証

private rootは`.cache/studies/new-strategy-validation/`。`s2-selection.json`、`s2-author-initial.*`、`s2-independent-initial.md`、`s2-independent-comparison.md`、`s2-final-narrative.md`、`s2-original-forecasts.json`を順に読む。原4社は`plan.md`のimmutable IDでapplication DBのThesis/Review/CAAへ戻る。保存一次資料は`primary/`、新規4社は兄弟`opportunity-coverage/primary/<ticker>/manifest.json`と`deep-manifest.json`、4231訂正は`s2-primary/`。非公開snapshot・取引数量・DB・credentialはGitへ置かない。

別環境でのtask実行には、原予測・選定・独立Review・manifestと参照primaryの必要ファイルを承認済み非公開保存先または人間による引渡しで用意する。`.cache`の自動同期を仮定しない。読めなければunverifiableで、後から原予測を再生成しない。canonical記録は正式storeからIDで解決し、原文を別の正本へ昇格させない。

使用CLIは`git status --short --branch`、`uv run baibai-engine position ledger`、`research --help`、`research watch --help`、`task --help`/`task add --help`、SQLiteは`file:<store>?mode=ro`と`PRAGMA query_only=ON`。exact保存queryは#1249 private `queries.json`、原判断参照は`plan.md`。taskのみpublic `task --db stores/application/baibai.sqlite add --title ... --kind ... --due ... --event-label ... --body ... --related-ref ...`を構造化引数で実行した。予測計算は既存`research/valuation.py`の`Projection`、`project_return`、`valuation_conditions`、`maximum_entry_price`を使い、production算術を複製していない。


既存valuation ownerを使うprivate小計算・fixtureで、3resolved社のBase/Down/Pmax/経済遅延をDecimal一致、2221のnull、decision後entry、休日の観測前寄せ、欠けcalendar、split後原1株と分配権利/受取/未払、unknownの0補完禁止、fixture DBのread-only拒否、凍結ファイル不変を7群で確認した。再実行は `PYTHONPATH=engine/src .venv/bin/python .cache/studies/new-strategy-validation/prospective_fixture_check.py`。fixtureの計算例は将来実績ではない。helper SHA-256 `4114d4fa0a61aa3fea56afe57df7f2bd1d9dab524aaeadd28ede15cb0c47567b`、詳細は兄弟private `helper-verification-2026-09-08.json`。

full local gatesはPythonのfrozen全group sync、Ruff format --check/check、mypy、lint-imports、全drift、pytest -n4 --cov（2,683 passed / 8 skipped、85%）、Bandit、locked全group pip-audit、frontendのnpm audit/ci/lint/build/test（192件）、edgeのaudit/ci/types check/typecheck/test（37件）とWrangler deploy --dry-runを通過。マクロの実装・method・registry変更はない。制限環境のpytest終端待機とnpm DNS失敗は通常local実行条件で再実行し、クラウドへ試行を出していない。

完了照合ではS0〜S3、独立初回保存、全4社原予測と追跡、private引渡し、後続境界を確認。反証Reviewで現在quote不能の引継ぎと2221の観測範囲を修正した。現在価格点検だけは未評価2社をself-contained #1266へ、原判断の事実訂正は#1267へ移し、本提出で実施済みとはしない。

最終quality gateは主の反証と独立product 1名の再確認でPASS。未解決blocker 0。AP-01/02/03/04/05/06/07/09/12を再照合し、事実/評価、時点/株数、既存判断/研究draft/取引、資産総額/可用cashを分離した。
