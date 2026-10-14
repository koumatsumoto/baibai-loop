---
title: "株式併合・売渡請求から現金公開買付けの資料確認へ進む"
summary: "6881の通常EDINET取得から600円の現金exitを検証し、JPX理由による候補の取り落としを修正する。"
doc_type: measurement-record
status: active
---

# 退出資料の候補範囲（#1481 H3）

価値tierはT1。JPXの廃止理由が最終手続を表し「公開買付」を含まない場合、現行収集器は届出・成立・現金対価の資料確認へ進まなかった。理由は資料を探す候補条件とし、現金exit確定は従来の証拠条件で決める。較正を成立させるための値補完、会社別の手入力、基準緩和は行わない。

未価格exitの4銘柄から、現在も無料の公式資料を取得できる最も新しい6881 キョウデンを選んだ。結果returnや較正改善による銘柄選択ではない。公式[IRライブラリー](https://www.kyoden.co.jp/corporate/financial-results/library/)と8/10の通常EDINET一覧から、普通株68810、EDINET E02035を確認した。JPXの保存rowは2023-10-26・「株式等売渡請求による取得」で、現在の[会社公式ページ](https://www.kyoden.co.jp/corporate/financial-results/)にも同日・同手続の非上場化を確認できる。

| 一次資料 | normalizer / parserで確認した事実 |
| --- | --- |
| [S100RO4C 公開買付届出書](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100RO4C.pdf) | 2023-08-10、株式会社クラフト E30299→対象E02035。普通株1株600円、cash only。買付期間8/10〜9/25、決済開始予定9/29 |
| [S100RWTX 公開買付報告書](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100RWTX.pdf) | 2023-09-26、同じ買付者・対象。応募株全部の買付を行う成立報告 |
| J-Quants保存bar | 2021-04-30以降の6881にfactor!=1のeventなし、最終bar2023-10-25、廃止後barなし。普通株1株の価格basisとの不一致なし |

EDINETProviderの通常`backfill_document_identity`で、届出日からJPX廃止日まで全78日（2023-08-10〜2023-10-26）の一覧17,868行を隔離した検証market storeへ保存した。その同じ索引を`read_tender_offer_filings`が読むと、本件240/270は上記2件で、同期間内の訂正・取下げはない。通常`download_csv_zip`→`read_document_blocks`→既存cash/price/outcome parserを通し、任意の価格やdocIDをDBへ手で登録していない。

同じJPX row・EDINET索引に対し、変更前はcandidate 0 / resolved 0 / document read 0、変更後はcandidate 1 / resolved 1 / document read 2となった。導出値はticker6881、600円、JPX廃止日2023-10-26、offer S100RO4C、result S100RWTX。[検証集計](./normal-path-proof.json)に残す。document read件数はproviderへの読取呼出数で、cache hitを除いたHTTP request数ではない。

exitの日付は既存contractのJPX上場廃止日であり、9/29の公開買付け決済開始日や、個々の保有者への実送金日とは別である。この変更で入金ledger・課税・配当・将来returnの算式は追加していない。対象案件の宣言された届出〜廃止期間を通常取得した再現であり、2014等の別案件や全廃止銘柄の全期間資料を復元したとは主張しない。検証storeを本番L1として発行していない。

採用した修正は、候補理由に株式併合・売渡請求の表記を加えたものに限る。届出・訂正・取下げ・成立報告・target identity・普通株の一意な円建て価格・cash onlyの判定は維持する。summary/CLIも候補件数を`candidate_delisting_count` / `candidates`と呼び、現金exit確定数と区別する。

過去資料には公開inspection期間やCSV取得可否の制約がある。本件は一覧・CSVとも通常取得に成功したが、3376/6815/9422の同等再現や、現在の保存索引にない他案件の取得成功は未確認である。資料や一意なcode対応が取れない案件を価格0・全損・本件価格で埋めず、通常の未解決と理由を残す。残るH1/H2は[#1491](https://github.com/koumatsumoto/baibai-loop/issues/1491)、[#1492](https://github.com/koumatsumoto/baibai-loop/issues/1492)へ分離した。

回帰は各squeeze-out表記で既存cash証拠を通すこと、理由だけでは値を作らないこと、非現金を拒否すること、既存の撤回・複数価格等の拒否を含む。完全gateとCIは提出PRへ記録する。本番store・35/38の正式較正・published artifact・新context採用は変更していない。追加課金・認証変更・制限回避・AI呼出はない。
