# ログイン不要の外部予想・公開企業調査の選定（2026-10-22）

## 結論と範囲

#1514 / #1564。みんかぶを履歴取得へ追加し、証券リサーチセンター（SRC）とフィスコをResearch時の公開PDF参照へ追加する。Investing.comは検証を行い、日次の第二consensusとしてではなく、個別の評価変更を調べる補助候補とする。四季報は最新のログイン不要条件により保留。SBI/ウエルスアドバイザーは #1563。

ユーザーはみんかぶへ問い合わせ、利用して問題ないと確認したと明示した。個人利用の保存を前提とし、公開再配布、認証回避、追加契約を許可されたとは扱わない。原HTMLや実データの数表はGitに置かず、テストは合成表を使う。今回の比較は取得可能性と役割の検証であり、超過収益や判断改善量の検証ではない。

## 実物確認と反例

### みんかぶ

認証情報・Cookieの持込みなしの通常HTTPで、[7203](https://minkabu.jp/stock/7203/analyst_consensus)、[4436](https://minkabu.jp/stock/4436/analyst_consensus)、[8306](https://minkabu.jp/stock/8306/analyst_consensus)の200応答を取得。4436はクラウドブラウザのログインしていない画面でも確認した。業種は自動車・IT・銀行を含むが、3件を全市場coverageの証明にはしない。[2915](https://minkabu.jp/stock/2915/analyst_consensus)と無効コードは404だった。404からアナリスト不在を断定せず、HTTP失敗として記録する。

- 評価の5区分人数、アナリスト目標、会社予想との対照表を確認した。
- 上部の総合目標は、アナリスト欄と同じとは限らない。独立した証券アナリスト欄だけを読み取る。
- 表示されていない項目のチャート内部0、表示値と内部値の精度差があった。内部データから欠測を0へ変えたり、小数精度を水増ししたりしない。
- 表示日、業績予想表の最新欄の日付、実受領時刻を区別。これらを個別アナリストの発表日時と同一視しない。
- 原評価元・株数basis・指標別の予想人数の完全な対応は未確認。rating人数をEPS等の人数へ転用せず、footerの複数provider列挙から原評価元を決めない。
- 翌日以降の履歴は実受領から蓄積する。今回見えた1週間前等の比較値を当日の受領rowへbackfillしない。

[公式のアナリスト予想説明](https://support.minkabu.jp/hc/ja/articles/360048893593-%E8%A8%BC%E5%88%B8%E3%82%A2%E3%83%8A%E3%83%AA%E3%82%B9%E3%83%88%E3%81%AB%E3%82%88%E3%82%8B%E4%BA%88%E6%83%B3%E6%A0%AA%E4%BE%A1%E3%81%AB%E3%81%A4%E3%81%84%E3%81%A6)と公開表の表示を対照した。集計ラベルから未公開の算式を逆算しない。常設の保存契約は[データソース](../../../docs/reference/data-sources.md#external-assessment-acquisition)が所有する。

### Investing.com

[東京7203](https://jp.investing.com/equities/toyota-motor-corporation-consensus-estimates)では通常HTTP 200、[東京8306](https://jp.investing.com/equities/mitsubishi-ufj-financial-group-(to)-consensus-estimates)では公開ページの評価人数・12か月平均目標/レンジ・個別証券会社名・評価変更日・新旧目標を確認した。ADRや他国上場の同名銘柄を代用していない。

[2307](https://jp.investing.com/equities/cross-cat-co-ltd-consensus-estimates)の公開予想ページには数値評価がなく、概要は未対応と表示。[4783](https://www.investing.com/equities/nippon-computer-dynamics-co-ltd-consensus-estimates)は検索経由の読取が古かったため、通常HTTPで再取得し、公開本文の予想値を確認できない状態を照合した。ページ自体があることと評価が収録されていることは別である。

- Fair Valueはロックされた別項目であり、無料consensusと混同しない。ログイン・課金・ロック解除はしていない。
- 7203/8306では買い区分をまとめた人数と、表示の丸め差以内の平均目標がみんかぶと整合する。小数表示の精度差まで完全一致とは扱わない。集計母集団や原データ契約が同一であることまでは証明できないが、別サイトを独立票として数える根拠にもならない。
- 現時点の増分は、証券会社別の変更日・変更主体・新旧目標を追えること。理由本文の入手や無料の全期間履歴まで確認済みとはしない。
- [公式consensus説明](https://www.investing-support.com/hc/en-us/articles/360000236178-What-is-a-Consensus-Estimate)も全銘柄の収録を保証しない。Pro製品のprovider説明を無料consensusへ流用しない。

したがって、今回の日次実装はみんかぶに集中する。Investingを追加するなら、単なる平均値の複製ではなく「誰が・いつ・何を変更したか」という差分の価値と維持費用を対象にする。

### 公開企業レポート

- SRCの[151Aレポート](https://holistic-r.org/adm/wp-content/uploads/2026/09/151A260918.pdf)は17ページ中の関連ページを確認。会社計画と独自予想、理由が区別される一方、当該レポートでのカバレッジ終了も記載される。[4885](https://holistic-r.org/c_info/4885/)と[7800](https://holistic-r.org/c_info/7800/)の一覧には古い資料しかなく、検索結果の存在を現在の予想としない。
- [フィスコの企業調査例](https://www.fisco.co.jp/wordpress/wp-content/uploads/FISCO/keiwa20261020.pdf)は24ページ中の関連箇所と末尾の免責を確認。発行企業の依頼・報酬関係を踏まえ、事業理解に用いるが独立した買い票へ変換しない。
- 全PDFの全文精読を主張しない。実装は既存Research skill・workspace案内・PDF reader・source/fact/calculationの利用であり、専用の収集基盤や日次全文保存ではない。

## 実装レビューの焦点

L1原本、型付き履歴、原本からの再構築、cutoff、固定cohortの直列取得、stage往復、固定release公開/読戻し、Research入力と生成Web契約まで接続する。公開後の変更partition読戻し対象から新しい予想datasetが漏れていた点は修正し、実receiverからstage/publicationまでの合成テストを追加した。

market schemaは47から48へ変わる。旧DBをそのままhydrateしてコードだけを配備しない。[運用手順](../../../batch/OPERATIONS.md#market-v48-cutover)の明示的な一回限りのコピーで、既存値・原本・Lake originを保持して切り替える。通常reader/day jobにmigrationは追加しない。切替済みとはまだ扱わず、最終HEADのCIと本番stepを別々に確認する。

外部評価を独自FVへ自動投入せず、欠測を購入禁止にせず、媒体数を確信度に換算しない。会社予想と外部予想の相違は、既存の事業・利益・資本・時間の仮定を検証する材料にとどめる。
