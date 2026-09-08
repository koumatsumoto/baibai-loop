# Discovery・Triage・調査枠の取りこぼし診断

価値tier: T1 — 入力の欠陥と研究枠の順位問題を分け、投資判断を変える次の作業を一つに絞る。

本番の4 Approach×top20とresearch/skipを維持する。先に直すべき具体的な入力計算の欠陥は4812の実績TTMで、[#1265](https://github.com/koumatsumoto/baibai-loop/issues/1265)へ分離した。Research価値YがReview Set外にも存在することは確認したが、それだけでは入口拡張の根拠にならない。blind比較ではAの4439がOの2221を置換し、入口拡張より同じ集合内のpriority理由を点検する仮説が残った。配分採用は0社。

## W0/W1: 結果前の固定と母集団

[計画](plan.md)をcommit `1dd72352`で固定し、#1263 S1のrubric点検後に`95f19452`、追加深掘り前にC*を`eb2962c9`へ記録した。最新取得mainは`317ca38c413c89872c36f8d7229eabd48a7f9051`。結果を見て日付・seed・層・ticker・top20を変更していない。

| 項目 | 固定値 |
| --- | --- |
| as-of / cutoff | 2026-09-07 / 同日23:59:59.999999 JST（run_atとの早い方） |
| run / 実行時刻 | `run-revision-7ab9668455a845cb878db12a5f7bdbce` / 2026-09-08 08:26:05.287906 JST |
| Review Set | `review-set-20260907-a14dfd52d1d9` |
| 最新local canonical Triage | `research-triage-20260907-5a886e2984a8b4f7` |
| method | `multi-valuation-v4` / `3e4865e072f6559c5873072e65f5b418722b5d096988bbb7caa7611c10b67145` |
| rules | `c525a6c55309450f` / `method/screening/rules/2026-09-03T161939+0900.yaml` |
| store schema | application20 / runs5 / market25 |

localで取得できる最新canonicalを使った。cloud最新性や未報告broker状態は未検証。masterと当日raw barは各4,434、Analysisは3,705。分析の無い729社は確定policy除外Xに属し、Review Set内だけから全母集団を再現したわけではない。Xは確定不合格を先に判定し、残りをcommon成立Uと不明Dに分けた。

| 集合・層 | 母数 | 抽出n | ticker（層内hash順） | Y / N / U |
| --- | ---: | ---: | --- | --- |
| U: A research | 77 | 8 | 8887,9658,6675,6199,4439,3660,7279,9145 | 8 / 0 / 0 |
| U: B skip | 0 | 0 | 空、補充なし | 0 / 0 / 0 |
| U: C native通過・集合外 | 2,275 | 8 | 6752,3962,6463,2432,4970,7956,3197,4960 | 8 / 0 / 0 |
| U: D0 全native不成立 | 48 | 8 | 4499,4376,4812,2160,8766,6740,4575,4588 | 8 / 0 / 0 |
| D: common/分析不明 | 149 | 4（補助） | 7383,6579,350A,4890 | 4 / 0 / 0 |
| X: 確定policy除外 | 1,885 | 0 | 評価対象外 | 未評価 |
| O追加（主標本外） | — | 4（補助） | 6419,4231,2415,2221 | 4 / 0 / 0 |

`77+0+2,275+48=2,400=U`、`U+D+X=4,434`、Review Set=`A∪B`、Triage全77 exactly onceを確認。X理由は時価総額1,118、上場日数26、JPX flag31、市場範囲728、分類外sector542で重複あり、単純合計しない。X内のcoverage不足750は729のAnalysis欠落と21のcommon fact欠落で、主除外理由と別記した。X内に価値が無いとは判定していない。

common/native/orderは`screening/discovery/review_set.py`の`_common_eligible`、`_ordered_eligible`、`_reinvestment_values`等の実ownerを呼び出して再構成した。native母数はcurrent2,308、normalized1,880、asset1,086、reinvestment105。各top20の80 nominationは77 uniqueへ統合し、canonical membershipと完全一致。ADV/E[r]を新しいfilterへ使っていない。

seedは`baibai-1249-v2|<run_revision_id>|<ticker>`のUTF-8 SHA-256 hex昇順、同値ticker順。Oはpriority先頭から同時点の報告済保有・予約を除いた4社。C*にも同一資本条件を適用し、純粋研究価値ラベルには除外を持ち込まない。実際に人間が選択したResearch SetとOを同一視しない。

## W2: 独立ラベルと資料の見落とし

#1263 S1完了の2026-09-08 20:21:28 JST後に同じrubricで開始した。所属・native順位・Triage rationale/priority・原Thesis/CAA・将来株価を渡さない独立2contextで一次資料から判定。ticker/会社名は見えるため完全匿名でもLLM知識の遮断でもない。Yは具体的な価値経路と判断を変える次の問い、Nは関連事実からその経路を否定、Uは研究価値自体を判定できない重要欠損等。購入可能、黒字、確定触媒、完成FVをYの必要条件へ追加しなかった。

主標本24・D補助4・O追加4の32社は全Y。初回ラベル不一致0、追補20社も不一致0、未解決ラベルU0。ただし同意は正解の証明ではない。**初回の4812はTOBを見落としており、事業成長を保有し続けるという論拠を撤回した。** 最新短信と説明資料だけでは重要開示を取りこぼす反例である。

[8月28日TOB](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260828/20260828527772.pdf)、[追加答申](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260828/20260828528106.pdf)、[8月31日訂正](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260831/20260828528125.pdf)、[配当修正](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260828/20260828527533.pdf)を双方が再読した。原価格2,852円に対し予定対価2,880円、差28円（0.98%）。11月開始予定・20営業日、最低9,340,400株、日中EU clearance等は未充足で、TOBは未開始。契約最終合意の訂正と翌3月末解除条項を読み、架空の対抗提案を上値根拠にしない。研究の問いは「開始/成立条件・資本拘束期間と不成立時価値が、この小さな確定対価差に見合うか」へ変更した。Y維持は購入肯定ではない。

同じ固定32社の当時重要開示を追加点検し、他19社32資料も両者へ同じ深度で追補した。初回メモ・rubric・C*を上書きせず別revisionに保存。特に3660の資金使途/希薄化、350Aの借入・設備、6740の工場売却、8766の分割、4588の承認後発売を反対仮説へ戻した。追加を読んだ後に有利なtickerへ入れ替えていない。取得不能で全case不能となった件数0。4376の任意business資料44頁は画像本文未読で、最新短信・補足の実読で研究価値を判定した。6752の説明資料PDFはHTTP403だったが同社同PDFのweb本文37頁を保存し読んだ。全文取得と有報全項目の実読は区別している。

主の追補統合は`labels-final-supplemented.json`に保存。32社の具体的質問と反証を以下に示す。初回・追補の両独立本文と全source/pageはprivateに残し、要約だけで将来の正式Researchを代用しない。

| case / ticker | 判定を変える問い（全Y） | 最強反対仮説 | 公開根拠 |
| --- | --- | --- | --- |
| oc-01 / 6752 | AI周辺製品の実需・価格・能力増強後ROICは持続するか。IRA現金受給と為替を除いて全社FCFの増分が家電・車載投資を上回るか。 | 需要前倒し・円安・補助金依存が持続利益を過大に見せる。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260730/20260730502815.pdf) |
| oc-02 / 3962 | 無料を含む導入数のうち有料化率・単価・継続率はどれほどか。ポイント廃止後のふるさと納税利益減と優待費用を上回る公共DXの利益が残るか。 | のれん集中、利益のQ3偏重、優待費用と買収の継続で株主取り分が薄い。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260813/20260813519282.pdf) |
| oc-03 / 6463 | アジア利益の親会社帰属と送金可能額はどれほどか。顧客値下げ・賃上げ・閉鎖費を控除して維持投資後の回収余力が継続するか。 | 顧客値下と賃金上昇、内燃機関関連需要低下、非支配持分が親会社利益を圧迫する。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260807/20260807513908.pdf) |
| oc-04 / 2432 | スポーツ・新施設のCFがゲーム減益を補うかという初回の問いを、買戻し後の自己株控除株数で評価すると一株価値は増えるか。売却資金の再投資と還元の割合は適切か。 | 消却だけでは新たな現金還元が発生せず、ゲーム減衰と新規投資の浪費を救わない。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260805/20260805510204.pdf) |
| oc-05 / 4970 | 一過性約9億円を除く材料の増分粗利・稼働率が、下期原料高と増産固定費を上回り、次期にも設備回収CFを生めるか。 | 通期増益8億円の大半を一時要因が説明し、下期販売増も正常利益増へ転換しない。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260807/20260807515474.pdf) |
| oc-06 / 7956 | 国内の高付加価値化と海外利益成長は為替・一時販促を除いて持続するか。出生減下でも顧客単価と継続購入で資本を増やせるか。 | 中国市場縮小と販促費、在庫増加が円換算増収の裏で経済利益を削る。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260806/20260805510885.pdf) |
| oc-07 / 3197 | 上方修正の既存店客数・単価増が新しい期の賃金・食材費にも勝てるか。27円配当と転換投資・借入返済を両立できる実力CFはどれほどか。 | 上方修正は営業利益であり、賃借料・出店改装資金と需要反動を引くと株主CFが増えない。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260813/20260813519030.pdf) |
| oc-08 / 4960 | 製品価格の値戻しが原料価格低下より早い期間に、受託稼働と休止費削減はどこまで支えるか。下期正常化に依存しない収益の下限はどれほどか。 | 販売数量の前倒しと逆方向の売買価格が休止費用削減を打ち消し、薄利が続く。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260810/20260805510552.pdf) |
| oc-09 / 8887 | 未行使3058000株の行使による資金約2.23億円と一株希薄化、買戻し約1億円、蓄電所/ホテル保有投資を一体で見ると、物件利益・管理CFは既存株主の残余価値を増やすか。 | 買戻し264,500株より大きい低行使価額の潜在株で取り分が薄まり、資金不足と在庫長期化が続く。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260825/20260825525944.pdf) |
| oc-10 / 9658 | 要員偏在はスキル不一致や価格の低い案件構成という恒常要因か。受注残の立上げ時期とBPO配置転換により、移転費を除いても単位人件費当たり粗利が回復するか。 | 受注と人材が構造的にミスマッチで、上期減額後も通期据置を支える下期利益が生まれない。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260813/20260813519460.pdf) |
| oc-11 / 6675 | 138名の退職で失う技能・外注代替費を引いた年間人件費削減はどれほどか。追加退職費、税・移転投資後の土地売却資金を還元と重点事業に配分して価値を増やせるか。 | 138人への支払増と戦略再配置の失敗で、土地売却資金が再編に消え本業の利益も落ちる。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260824/20260824524885.pdf) |
| oc-12 / 7383 | B2Bとatoneの増加分は回収期間・資金調達費・新規顧客の信用損失を控除してどれだけ利益を残すか。貸倒改善は一時的な回収か恒常的モデル改善か。 | 加盟店・債権信用リスクや貸倒引当不足が薄い手数料を吸収する。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260814/20260814521233.pdf) |
| oc-13 / 6579 | 黒字motoを除いた残存主力広告・EGG・ウルテクの粗利益は販管費を上回れるか。配当34百万円を連結の外部資金増と扱わず、6百万円売却収入と失う年9百万円利益を含めると集中策は一株価値を増やすか。 | 黒字小規模子会社を売り、単体配当を外部CFと誤認すると残る広告事業の赤字を過小評価する。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260820/20260819523361.pdf) |
| oc-14 / 4499 | レガシー送客効率と金融DXの人員配置改善による511百万円の予想利益改善を分離し、後者が採用・開発の先送りでないか検証できるか。残余FCFはどれほどか。 | 損失縮小が投資遅延だけで有償金融DX収入が増えず、既存事業利益が開発費に消える。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260814/20260814521027.pdf) |
| oc-15 / 6199 | クラウド定着支援の単価・稼働改善が賃上げを上回るかを、買戻し用途・支配株主売却・SO行使後の自己株控除分母で評価すると、一株CF増は残るか。 | 取得自己株のM&A等再交付、支配株主への流動性提供、外注費上昇で株主還元効果が薄い。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260806/20260806512092.pdf) |
| oc-16 / 350A | RE/ASの手数料成長を、電力立替需要・枠手数料・借入利息と蓄電池100億円投資を控除して見ると、限界ROICと一株CFは改善するか。2028/6の一括返済を運用CFで賄えるか。 | 容量成長が手数料単価低下に負け、貸倒・精算資金と保有蓄電所投資で資本集約度が上がる。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260724/20260724599394.pdf) |
| oc-17 / 4439 | 電力調達高騰時の転嫁条件と顧客解約率はどう動くか。販売代理店費・増員費を含む新規契約の回収期間が継続利益を支えるか。 | 調達価格変動や獲得費先行が契約成長の経済利益を奪う。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260715/20260713591957.pdf) |
| oc-18 / 4890 | TLG-001の層別結果は事前規定と再現性を持ち導出可能か。既存TLM契約の次回収入条件と追加臨床費用から、希薄化後一株当たり価値が残るか。 | TLG-001の全体有意差なしは技術一般化を支持せず、前倒しマイルストーンは恒常収益ではない。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260812/20260810516798.pdf) |
| oc-19 / 3660 | BtoBと店舗の増分CFは、予約権取得31.5億円・買戻し最大28億円を支出後も成長投資を支えられるか。失う調達資金と回避希薄化を比較した既存株主の正味便益は何か。 | 潜在希薄化解消に高額現金を払い、小売在庫と店舗投資がブランド支援利益を吸収する。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260813/20260813519003.pdf) |
| oc-20 / 7279 | 国内初受注の契約数量・単価・原価条件から、12億円投資とACT買収後の維持投資を回収できるか。樹脂内製と機械再使用の便益は立上げ費・顧客値下げを超えるか。 | 国内新受注が低採算で、閉鎖設備移転・立上げ・既存海外不振が負ののれんの見かけを消す。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260907/20260904532085.pdf) |
| oc-21 / 4376 | 課金店舗純増は単価・解約率を含めて持続するか。暮らし・相談の実力CFは他事業の投資負担と持株会社費を控除して株主に残るか。 | 買収の増収効果に比べ既存事業の再成長が弱く、投資事業への資金流出が続く。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260814/20260805510073.pdf) |
| oc-22 / 4812 | 競争法手続の対象市場・重複事業と開始条件、応募下限の構造を検証すると、2880円受領の時期と不成立確率はどの範囲か。不成立時に残る単独事業の持続利益価値と比較した期待損益は、拘束期間・費用を含め投下を支持するか。 | 非公開化により少数株主は長期成長を享受できず、条件未充足・遅延による損失が限定対価を上回る。DCF上端や条項のみから値上げ・対抗TOBを期待する根拠はない。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260828/20260828527772.pdf) |
| oc-23 / 2160 | あゆみ買収を含む希薄化後株数と借入金利を用い、F351収益ゼロ・先行投資増のケースでも既存医薬品CFは親会社一株価値を増やせるか。 | 売上の74.3%増額は買収範囲の拡大であり、利益非開示の支出と調達負担が取り分を相殺する。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260824/20260824525256.pdf) |
| oc-24 / 9145 | 東海SCMの一時費用と恒常的な協力会社値上げを分離し、契約単価改定と生産性回復で残存拠点利益を確保できるか。通期据置を支える案件別月次採算は何か。 | 一時費用という説明だけでは恒常原価増を補えず、通期下期の利益回復が未達となる。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260814/20260814520911.pdf) |
| oc-25 / 8766 | 分割調整後の株数・配当・優待費用を同基準に揃え、海外引受利益の持続性と政策株縮減で必要資本を超える株主還元がどれほど増えるか。 | 名目の株数・優待の印象に対し、損害インフレと準備金・自然災害が経済価値を侵食する。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260825/20260825525826.pdf) |
| oc-26 / 6740 | 資産譲渡で回収する税費用控除後現金と移転負担を区別し、残存車載・産業事業のCFで利息・借入返済を賄えるか。新たな希薄化後に普通株へ残る価値はどれほどか。 | 売却代金が債権者・再編費に吸収され、営業損失に巨額利息が加わり普通株の残余価値がない。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260824/20260819523146.pdf) |
| oc-27 / 4575 | Phase2の対照・サンプル・効果量と次相設計を検証すると、45–50億円と希薄化を負担する価値があるか。製造規制対応と資金確保は成功確率をどれだけ変えるか。 | 小規模試験の見かけ効果、製造規制対応の遅れ、保有現金を超える試験資金で大幅希薄化となる。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260812/20260808515604.pdf) |
| oc-28 / 4588 | 販売開始後の施設採用手続・対象患者・供給能力から実際の処方数量がどれほど立ち上がり、提携先分配と原価を控除する当社の反復利益が研究費を賄うか。 | 保険適用・発売は実投与数や正の粗利を保証せず、供給・医療現場の採用が遅いと資金不足となる。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260821/20260819523068.pdf) |
| oc-29 / 6419 | 新紙幣・スマート機導入を除いた更新、保守、クラウド収益はどれほどか。大手集約時の継続契約と価格条件で設備周期後も余剰資本を回収できるか。 | ホール閉店・設備投資一巡で収益が減衰し、余剰資産が低採算買収へ向かう。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260730/20260723598585.pdf) |
| oc-30 / 2221 | 765百万円支出後の現金と証券配当収入で、原料米・増強設備負担を賄えるか。買戻しが低い本業収益の補填と競合せず一株回収価値を増やすか。 | 買戻しが原料高対応と運転資金を圧迫し、旺旺資産・配当が下がれば還元の持続性がない。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260819/20260819522784.pdf) |
| oc-31 / 2415 | 日本語・社会人教育の増分粗利は全日制生徒減と賃金上昇を上回るか。教育から就業への実際の移行率・獲得費削減が示す経済価値はどれほどか。 | 採用難・学生獲得競争で費用増が先行し、補助金が経常利益を押し上げて本業鈍化を隠す。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260807/20260806513158.pdf) |
| oc-32 / 4231 | 日本の値上げは原材料の次段階上昇にも追随できるか。産業用ホースの伸びは米州自動車減益を構造的に相殺できる規模と粗利を持つか。 | ホンダ集中と米州採算悪化、設備投資で見かけの余剰資金が拘束される。 | [一次資料](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260810/20260807514125.pdf) |

## W3: owner実入力に戻した原因

原因はYの研究価値と、その工程で実際に止まった理由を別々に判定した。主標本の確認Yは原因6（research内のO枠外）8件、原因3（native順位）8件、原因2（全native不成立）8件。補助D4は原因1の必須fact不足。Bが空なのでskip境界の性能は未検証。原因4のunion重複を主原因としたcaseは0。80→77の重複があるだけでは、今回のどのcaseを取りこぼしたかを説明できない。

A8件のpriorityは8887=65、9658=76、6675=46、6199=42、4439=25、3660=19、7279=53、9145=67。各社はいずれかのtop20に届いておりDiscovery漏れとは呼ばない。6675/6199には実際の既存v4調査があり、「Oに無い」を「調査されたことがない」へ読み替えない。その後の企業評価・価格・配分の判断は#1263で分離した。

| C ticker | 実native順位の例 | 主原因 |
| --- | --- | --- |
| 6752 | current1,732 / normalized1,459 | top20の深さ |
| 3962 | current234 / normalized190 | 同上 |
| 6463 | asset85 / current674 | 同上 |
| 2432 | asset396 / normalized1,786 | 同上 |
| 4970 | current1,883 / normalized1,571 | 同上 |
| 7956 | current1,968 / normalized1,569 | 同上 |
| 3197 | current2,052 / normalized1,799 | 同上 |
| 4960 | current2,233 / normalized1,787 | 同上 |

D0は入力欠損と経済値の失敗が混在する。正の利益を要求するApproachがあることだけから全社を排除したと判断しない。

| D0 ticker | 当時の入力とnative不成立の理由 |
| --- | --- |
| 4499 | forward/trailing/normalized PER・asset値なし、営業−6.40億円、reinvestment組立不能 |
| 4376 | PER/assetなし。reinvestmentは値が組めるがreturn0.15456<sector床0.16898、margin0.04290<0.04910等で失敗。欠損だけの例ではない |
| 4812 | PER/P/Sの入力欠損。以下の実績TTM問題を確認。forward EPS欠損は別の契約 |
| 2160 | PER/normalized/asset/debt/cash等の欠損とsales_yoy−0.0257等。売上成長仮説とnative入力を混同しない |
| 8766 | 保険業のasset適用範囲外、PER/normalized/P/S等の欠損。金融業へ非金融モデルを強制しない |
| 6740 | 営業−12.14億円、asset ratio−0.1772、P/S sector gap+0.4352、他の入力欠損 |
| 4575 | 営業−13.58億円、debt/asset/売上成長等の欠損。現金22.02億円を将来研究費控除なしに価値へ置かない |
| 4588 | 営業−7.67億円、normalized/assetなし、P/S gap+151.55。承認後の価値変化の問いと現時点native不成立は両立 |

**confirmed defect: 4812の実績TTM。** `engine/src/baibai_engine/screening/metrics.py::_ttm_value`の実績入力選択は8月28日の配当のみSummaryを最新として選び、実績項目nullで計算を失う。一方actual rowsには2026H1・2025FY・2025H1が存在する。同じ当時storeで売上`88,649+164,865−80,239=173,275`百万円、NI`8,888+16,365−7,684=17,569`百万円を再現できる。実績TTMの契約に照らした局所欠陥として[#1265](https://github.com/koumatsumoto/baibai-loop/issues/1265)へ引き継いだ。修正後のnominationや投資採択を保証するものではない。最新forecast EPSがnullなら古い92.22円へ戻さないv23契約は正しく、forward欠損の救済を同Issueへ混ぜない。

**補助D4。** 7383/6579/350A/4890は自己株が730日windowの全財務行でnull（各9/9/5/10行、数値観測0）で、市価総額ownerが要求する「発行済−自己株」を作れない。株価415/353/852/293円、gross発行99,525,999/3,803,000/41,012,580/25,794,300株があってもgrossで代用しない。当時一次資料の自己株「−」を機械的な0とはせず、raw APIレスポンス未保存なのでingestion defectとは断定できない。数値0のXBRLか明確な記号定義を確認できれば回復可能性を再判定するが、今回Dを当初Uへ移していない。

率の主張は行わない。層内各8/8 Yを不均等な母数へ単純合算してrecallにしない。層別の母数と確認件数に限定し、探索的な市場全体の捕捉率や統計的信頼区間を出さない。B0・各n8・同日相場・広いResearch価値定義と共通source漏れにより、完全性や収益優位は認定できない。

## W4: 同じ最大4社の枠へ比較し直す

C*は24主標本YからOを除き、Oと同じ保有/予約条件を適用した集合を、所属・原priorityを知らない担当が選んだ。2026-09-08 20:42:54 JSTに4970/6675/4439/7279を固定した。失敗caseの補充なし。Oと同じcutoff、9月7日raw価格、最新短信/説明資料、有報の関連財務・CF・risk・capitalと重要資本開示を読み、主の経済評価と原結論未開示の独立Reviewを別々に作った。Oは#1263の4社を同一資料部分だけ再利用した。全8社を深掘りしたが有報の全項目監査ではない。

| ticker / 原価格 | 主のBaseと購入条件 | 独立Reviewと残る分岐 |
| --- | --- | --- |
| 6419 / 3,190円 | NI55億×10 / 17.394285百万株、分配150、h12/r10。末価3,161.96、年率3.82%、Pmax3,010.87 | 独立56億×9は要求未達。一方資産別探索約4,119円を収益法が十分認識できるか未解決 |
| 4231 / 1,051円 | NI22億×10 / 19.5百万株、38、h12/r10。末価1,128.21、年率10.96%、Pmax1,060.19 | 独立24億×9を同株数へ揃えると9.01%。顧客集中とcapex後CFを含む余裕が薄い |
| 2415 / 1,686円 | NI22億×9 / 10.377962百万株、71、h12/r10。末価1,907.89、年率17.37%、Pmax1,798.99 | 独立23.5億×8で11.66%だが主22億を8倍なら4.80%。利益と倍率の相殺を確証にしない |
| 2221 / 3,065円 | h/要求/Base/Down/returnはnull。資産探索約2,835–3,205円 | 独立静態約3,042円も税・用途・投資時系列が不確実。配当32を別加算するbasis不足は撤回 |
| 4970 / 10,680円 | NI32億×18 / 7.936738百万株、50、h12/r10。末価7,257.39、年率−31.58%、Pmax6,643.08 | 独立NI35億×18でも−25.21%。在庫一過性除外後の成長幅では現価格を支持できない |
| 6675 / 2,177円 | h/要求/Base/Down/returnはnull。EV/EBIT＋残余金融資産探索約1,315–2,573円 | 独立営業税後NI14億×10＋期間末資産137.22億の末価1,584.85/分配125は条件付きh12例。未知の使途を解消した事実ではない |
| 4439 / 792円 | NI25億×11 / 30.8百万株、13、h12/r10。末価892.86、年率14.38%、Pmax823.51 | 独立NI26.5億×10/n30.5mで11.35%。同主NI×10なら4.13%、同独立NI/n30.8mなら10.28% |
| 7279 / 2,589円 | NI40億×12 / 36.5百万株、53、h12/r10。末価1,315.07、年率−47.16%、Pmax1,243.70は一つの収益法 | 独立資産Base3,452.14/Down1,779.58はstatic、h12見返り未採用。両評価を平均せず別法未解決を保持 |

O4の経済評価・一次source・Downsideは[#1263](https://github.com/koumatsumoto/baibai-loop/issues/1263)のreportとprivate `s2-original-forecasts.json`に同一内容で保存。主は全4社の配分を見送り、正式提案・人間裁定・取引はしていない。新規2群を混ぜて正式CAAをpublishしていない。

**4970。** [Q1](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260807/20260807515471.pdf)と[説明資料p2,10–13](https://www.toyogosei.co.jp/news/2026/20260807_FY2026Q1financial_presentation.pdf)のFY営業58億円には在庫一過性約9億が入り、下期23億据置。NI会社38億から税後約6億を除いた主32億を正常利益とし、Q1を4倍しない。[有報p21,25,60,78](https://www.toyogosei.co.jp/news/20260625_yuho_76.pdf)の信越16.42%、cash37.28億・借入244.70億、年営業CF74.90−有無形capex48.14=26.76億と翌年設備44.60億を接続した。[淡路タンク17億](https://www.toyogosei.co.jp/news/20260805_Notice.pdf)をその予定に重ねて足さない。18倍で原価格10%に必要なNIは51.58億。Down18億×12/D30は−74.24%、24m経済遅延24億×15/D80は−34.26%。独立35億は数量/mix改善を3億認める将来仮定で、利益計算の訂正ではない。原価格では見送り、次の半期の在庫影響除外利益・稼働と投資回収が問い。

**6675。** [Q1](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260807/20260806511856.pdf)の土地益231.77億を持続NIにしない。[処分完了](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260724/20260723598532.pdf)67,800株を加え、known株数17,491,590へ訂正した。主初回・S1原Thesisの17,423,790はQ末値であり、原判断訂正を[#1267](https://github.com/koumatsumoto/baibai-loop/issues/1267)へ分離。h/FV null・見送り結論は変わらない。主のEV/EBIT8–10と独立の税後営業NI10倍は同じ倍率でなく、金融収益を除いた利益と余剰資産控除を揃えても使途の不一致が残る。[新工場/譲渡](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260605/20260604563324.pdf)・[中計](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260605/20260602559534.pdf)の投資が価値を生むことも失うこともあり、全額永久損失にしない。独立Downは期間末残余100億＋営業NI7億×8/D100、遅延24mは残余110億＋NI10億×9/D225という探索。主のnullを強制的な単一12mへ変えず、125円分配原資と期末資産を二重算入しない。

**4439。** [Q3](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260715/20260713591957.pdf)はOP24.82億・NI16.98億。[補足](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260715/20260713591960.pdf)の光139,998回線・電力69,477件、月解約0.67%/1.47%、stock89.9%を確認したが、SaaSと同一視しない。FY会社NI25.84億に対する主正常25億は一次業績に近い一方11倍は再評価を含む。原価格10%にNI24.03億@11またはPER10.573@25億が必要。[有報](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100X6GX.pdf)の10%超顧客なしとNTT供給依存は別概念。cash77.16−debt17.19億、年営業CF24.18−capex0.32=23.86億から保証金・獲得先行費を分けた。借入には純資産/経常利益等のcovenant。実績30,064,868株から信託売却を再控除せず、30.8百万はexact fully dilutedでなく将来交付の保守仮定。第6回SO22万株は2027/12行使開始でh12外。[D13](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260114/20260107530465.pdf)は未公表次FYの同額維持を仮定した権利basis、8月末7円の未払を別記し、2026年8月末既権利7円は除外。Down16億×8/D8は−46.52%、24m遅延22億×10/D21は−3.65%。解約・電力転嫁・獲得cashから25億超の正常利益を支持できるかを優先調査し、見返り10%超だけで配分を確定しない。

**7279。** [Q3](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260904/20260902530338.pdf)NI408.42億−負ののれん283.08億−税後株売却96.62億=28.72億で、株売却と買収会計を恒久利益から除いた。[補足](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260904/20260904531480.pdf)のACT売上784億/OP6.5億は既存事業成長ではない。[7月](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260803/20260729502140.pdf)と[取得完了](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260907/20260904532085.pdf)からQ後468,100株/12.108億円を両方控除しknown36,364,757株、36.5百万は将来余裕。cash738.46億・短期証券67.87億・投資証券323億・借入235.10億にNCI199.74億・退職給付71.04億と資金移動制約を対応させ、純資産2,161.22億を下値床にしない。[資本方針](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260710/20260709590838.pdf)と[2027–28再編](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260803/20260802505976.pdf)、[2028/12量産投資](https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260904/20260903531462.pdf)は12mで全効果を得られない。主Down20億×9/D40、遅延24m30億×10/D80は収益法stressで資産法への反証ではない。独立の資産毀損haircut/36m例も査定済み価値・法務確認済みとはしない。原価格配分を支持せず、資産法の帰属/使途と事業回復を分けて未解決とした。

主のC*初回は21:03:27 JST、独立初回21:06:03 JST、固定後照合を別memoへ保存。最終値は`cstar-final.json`。全社の資金・返済・CF・希薄化・顧客集中・構造衰退・governanceの7軸をprivate本文に保持し、未読の法的条項や未来条件をverifiedへ昇格していない。


### 出自を隠した最大4社の選び直し

別contextが8社のResearch・独立初回・照合済み最終pack全体を読み、2026-09-08 21:24:44 JSTに**2415 → 4439 → 6419 → 4231**を固定した。追加PDF実読なしの経済比較であり、一次独立検証は前段のResearch/Reviewが担う。元の所属・順位・W3は読ませていないが、作者の各社評価は見えるため、作者の判断からも完全blindとは呼ばない。pack SHA-256 `8cd2bdd362b32e886ff7f034bda1dd0a32bfc4a2368f19aadb0239d76abe3440`、比較MD `7e4a5da0aca38610fcd1ca10ebf7eb74ecc221dbae18a88d90aa489eafda2043`。時計観測から約316秒以上、実費不明。

| 結果 | 根拠と比較相手 | 原価格の資本判断 |
| --- | --- | --- |
| 2415 採る・1 | 前受・給与季節性・補助金から反復cashを検証する問いが限定的。4231より顧客分散 | 条件付き比較可、配分未支持 |
| 4439 採る・2 | 契約/解約・電力利益・獲得費からNI25〜26.5億の持続を確かめると原価格条件を横切る。2221の税・旺旺・大型投資の複数未知より追加調査範囲を絞れる | 条件付き比較可、配分未支持 |
| 6419 採る・3 | 低い収益法returnで落とさず、資産別法と正常利益の差を検証。2221/7279よりcash主体で事業との境界を絞れる | 資産帰属/運転留保の追加確認が必要、配分未支持 |
| 4231 採る・4 | 地域利益と維持投資の境界が判断を変える。7279より既存利益・株数・統合範囲が明確 | 顧客集中とCF余裕が薄い。4枠目を空ける異論も合理的 |
| 2221 劣後・5 | 税・同日旺旺時価・6年投資の利益化を同時に解く必要。6419より範囲が広い | h/FV unresolved、価値0でも永久rejectでもない |
| 7279 劣後・6 | ACT/PPA/NCI/地域cash/2028再編の複数未知。資産別3,452円を12mへ変換しない | 別法未解決、−47%だけで落としたわけではない |
| 6675 劣後・7 | 改革利益と残余資金の両方の具体化が要る。完了処分訂正だけでは資本判断を変えない | 原defer維持 |
| 4970 劣後・8 | 正常NI32〜35億に対し18倍で必要51.58億、現時点の実需確認だけでは遠い | 原価格では見送り |

固定後に出自を開示するとC*から入ったのは4439（A・元priority25）の1社で、Oの2221を置換した。6675/7279もA、4970だけがCだったため、C/D0拡張に今回の4枠改善の証拠は得られなかった。D0/B全体に良い候補が無いという意味ではない。主は4439のCF/契約情報と2221の資産用途の差を一次根拠へ戻して支持したが、これは問いの狭さと価格感度による**改善仮説**で、投資成績の改善認定ではない。選定4社とも配分採用0。全8社の反証・最小追加情報はprivate `w4-blind-comparison.*`に保持する。

## 調査負担とこの比較が答えないこと

このpilotは通常と同じ費用・情報量のA/B試験ではない。主標本24＋D4＋O追加4を先に確認し、8社を深掘りしてから4社以下へ選び直した。以下の取得・処理時間は重複・並列があるので足して人間工数や通常運用の所要時間にしない。料金・token総量を取得できず、AI費用は不明（0ではない）。

| 工程 | 取得/実読の範囲 | 実測と再作業 |
| --- | --- | --- |
| 初回資料取得 | 32社75source（74 PDF＋同社PDFのweb本文1）、O4追加11PDF。合計取得2,514 PDF頁＋web37頁 | 最初のPDF保存から13分48秒、初期検索時間を含まない。plain再抽出、4439文字化けのMuPDF補修、6752 CDN403代替。取得頁数は全頁実読数ではない |
| 独立W2 | 初回は両者が各32社・64 URLの短信BS/PL・事業説明を実読 | 初回全工程elapsed未取得。人間への銘柄/閾値照会0 |
| C*深掘り追加取得 | 4社20資料423頁、18新規/2再利用、6675既存有報再利用 | 5分02秒。独立初回実読sourceは4970=6/6675=10/4439=5/7279=10、年報は関連節 |
| 重要開示補修 | 4812の4資料88頁、他19社32資料55頁（24新規/8再利用）、7279月次1頁 | TOB/metadata確認3分12秒、他資料補修2分23秒。並行作業で4812の3PDF重複取得を記録 |
| 独立追補 | 両者が同20社36資料を確認、Y変更0だが4812仮説撤回 | B実測7分25秒、A開始/elapsed未取得。原文を上書きせず別revision |
| 独立深掘り照合 | O/C*とも原結論未読初回を保存後に作者と比較 | 主/独立の全調査時間とtoken料金は未取得。本文訂正・評価基準の再照合を実施 |

32社全Yは候補を大量追加する効果を測らない。特に独立2者の共通source漏れと評価仮定相殺が確認されており、同意率や採択件数を品質の十分条件にしない。主が所属を知った状態で統合した制約も残る。最終比較者には別contextと出自を隠したpackを用い、元のpriorityを読ませていない。

既存calibration48月次cohortの44時点はDiscovery fidelity unavailable、完全なのは2026年5〜8月で、今回rulesとも違う。現行方法の成熟3y/5y評価へ転用できない。過去JPX欠損は再取得で埋まると保証できず、過去のnegative急落lensやinsufficient inflectionを今回Yで肯定へ変更しない。新しいempirical parameterをproductionへ接続するには現行estimate-calibrationのdecision subject/method fidelity/horizon/metricを満たす必要がある。

## 再実行・private引渡し・検証

private root `.cache/studies/opportunity-coverage/`の`metadata.json`、`run.json`、`population.json`、`native-orders.json`、`draws.json`、`capital-scope.json`、`sha256.json`が固定断面。`queries.json`と`prepare.py`にexact読取queryと実owner呼出し、`verify.py`にpartition/union/Triage一致を保持。使用CLIは`screening --help`/`research --help`、read-only SQLite `file:<store>?mode=ro`と`PRAGMA query_only=ON`。保存されたfinancial rowとsource metadataを用いて原Analysisを再現したが、provider原レスポンスや後日backfill不存在まで証明したわけではない。

再読は`blind-cases.json`→`labels-a/b.json`→`labels-final.json`→`labels-a/b-supplement.*`→`labels-final-supplemented.json`→`w3-owner-causes.json`/`w3-input-diagnosis.*`→`cstar-selection.*`→`cstar-author-initial.*`/兄弟new-strategy-validationの`cstar-independent-initial.*`と`cstar-independent-comparison.md`→`cstar-final.json`→`w4-blind-pack.md`/比較結果。一次PDFと実読pageは`primary/<ticker>/*manifest.json`と各メモ。Oは兄弟の`s2-original-forecasts.json`と最終本文を参照する。canonical原判断全文やDB・取引数量・credentialをGitへ出さない。

別環境では凍結母集団・抽出/対応表・両独立判定・原予測・manifest/必要一次資料を、承認済み非公開経路または人間の引渡しで用意する。`.cache`の自動同期を仮定しない。欠ける場合はunverifiableとして当初断面を現在値から再生成せず、今回の結果を再現済としない。

実行したsmall helperの検証は `PYTHONPATH=engine/src .venv/bin/python .cache/studies/opportunity-coverage/fixture_check.py`（10件）、`python3 .cache/studies/opportunity-coverage/verify.py`、`python3 .cache/studies/opportunity-coverage/aggregate_fixture_check.py`（6件）。同run/seed、8未満/空層、重複ticker、missing native、複数Approachからの捕捉、wrong method/as-of拒否、unequal strata、U感度/分母0、補助混入防止、O/C*対称除外を確認した。実際の経済選定の良さをfixtureで証明したわけではない。兄弟prospective fixtureではprepareの実connectを隔離DBで呼び、read-only/query_onlyの書込拒否と反復非変更も確認した。統合manifest `helper-verification-2026-09-08.json` SHA-256 `f1c481a22d6bd06e72346b085207e68e30ceaf18ca27756b5c18df601bf8d416`。private集計helperのlabel Nが母数Nを上書きする試作上の衝突は`population_count`/`label_counts`を分けて補修し、fixtureで確認した。production欠陥に数えない。

full local gatesはPython frozen全group sync、Ruff format --check/check、mypy、lint-imports、全drift、pytest -n4 --cov（2,683 passed / 8 skipped、85%）、Bandit、locked全group pip-audit、frontend npm audit/ci/lint/build/test（192件）、edge audit/ci/types check/typecheck/test（37件）とWrangler deploy --dry-runがPASS。変更はdated plan/reportのみ、production runtime/method/schema/CLI/cloudの変更なし。AP-01/02/03/04/05/06/07/09/12を再照合した。


## W5: 維持するもの・後続・次の1作業

| 観測 | 今回の選択 | 後続と採用/中止条件 |
| --- | --- | --- |
| 4812 実績TTMを非実績最新行が遮る | confirmed defect、最小修正 | [#1265](https://github.com/koumatsumoto/baibai-loop/issues/1265)。実績の選択だけ直し、最新forecast欠損/配当と真の実績欠損を維持。機械値復元を購入機会と呼ばない |
| Aの4439が2221を置換 | priority/研究枠の改善仮説を一つだけ送る | [#1269](https://github.com/koumatsumoto/baibai-loop/issues/1269)。通常Triage資料の範囲で「次の少数の確認が原価格判断を変える問い」へ理由を縮約できるか。新gate/scoreなし |
| Cの4970は不採択、D0はC*に選ばれず、B0 | Discovery/skip境界を維持 | native depth拡張の有効性、D0からの枠改善、skip漏れは未証明。Y件数を拡張理由にしない |
| D4の自己株未観測 | 入力不足を未検証として保持 | 0の一次根拠または記号意味確認なしに補完しない。原API非保存なのでparser欠陥と断定しない |
| 6675の完了株数記載 | #1263側で訂正対象を分離 | [#1267](https://github.com/koumatsumoto/baibai-loop/issues/1267)。本番のimmutable判断は上書きしない |

後続仮説は初回as-ofから5 JPX営業日後の**2026-09-14以降の最初の完全canonical run/Triage**へ固定し、`task-20260908-follow-up-5`（期限9月15日、trigger9月14日）を重複確認後に1件登録した。非task全canonical tableの内容hashは登録前後不変。本文に問い・必要一次資料・保存先・引渡し・採用/中止条件を記録した。R2/publish/dispatchは行わない。

再現は初回と異なるtickerを必要とする。後続Issueでは初回32社をO/追加A候補の双方から対称除外する条件変更を次回の結果を見る前に固定し、従来の全ticker通常Oも参照欄へ残す。この変更理由は同じtickerの継続を独立再現と数えないためで、日付/seed書式/cutoff/層内抽出は同じ。対象不足なら補充せずinsufficient。新tickerでも原価格の具体的な問いで置換し、毎回全32社/8社の追加調査なしに通常資料内へ縮約できれば採用案へ進む。好みだけ、情報量追加への依存、定常負担増が価値に見合わない場合は中止。今回標本へ合わせた改訂を同じ標本で合格扱いしない。

**次の1作業は#1265の実績TTM選択の最小修正。** 将来の再現・成績を待たず、配当のみ行の追加で実績を失う反例と真の欠損維持をローカルfixtureで確定する。priority仮説の反復は上記dated taskへ引継ぎ済み。現在の選定・売買条件・正式判断・取引事実は変更していない。

未検証なのは収益優位、市場全体のrecall、cloud最新性、過去Discoveryの長期fidelity、Bのskip性能、初回Dの自己株回復、未読の全法務/資産用途、通常費用でのpriority改善再現。これらをPASSの事実へ置き換えず、今回の固定pilot・原因診断・8社比較・後続設定を完了範囲とする。

完了条件との最終照合とquality gateは、主の実装者視点の反証・実行検証と独立product 1名の確認でPASS。正式finding・未解決blocker 0。W0〜W5、固定標本全件、8社/4枠比較、必要な後続、限定した完了範囲を確認した。
