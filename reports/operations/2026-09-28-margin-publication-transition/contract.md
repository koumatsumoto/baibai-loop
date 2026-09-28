# 信用残高日次化のClientV2契約確認（2026-09-28）

## 判定と根拠

#973のactivation条件を確認した。基点mainは`be9a48ae`、利用SDKは`jquants-api-client 2.7.0`。

- [JPX移行実施告知](https://www.jpx.co.jp/news/1032/20260927-01.html)で9月28日の実施を確認した。
- [J-Quants API公式仕様](https://jpx-jquants.com/ja/spec/mkt-margin-int)で全東証銘柄の日次配信、9月25日申込分からの切替、Dateが申込日であることを確認した。
- ClientV2の`get_mkt_margin_interest(date_yyyymmdd="20260925")`は`GET /v2/markets/margin-interest`を使用する。SDKのdocstringは週次表記のままだが、実応答は新仕様と一致した。
- 実応答は4,261行、Dateは全件2026-09-25、PubDateは全件2026-09-28。通常株式コードへの既存の正規化後は4,254行で、probe保存済みの全銘柄・6残高・IssTypeと完全一致した。
- 除外された7コードは25935、50765、75505、92015、92025、94345、94346。既存のcommon-code判定による除外であり、日次化で新たな母集団条件は加えていない。
- 応答列はPubDate、Date、Code、IssType、ShrtVol、LongVol、ShrtNegVol、LongNegVol、ShrtStdVol、LongStdVol、ShrtVal、LongVal、ShrtNegVal、LongNegVal、ShrtStdVal、LongStdVal。
- 保存対象の6残高は株数で、Dateをbalance_date、Codeをticker、IssTypeをissue_typeに対応させる。追加の金額列とPubDateは今回の既存schemaの保存対象外である。

## 実行結果

指定のbounded probeはexit 0、4,254行を保存した。続くone-shot verifierもpassとなった。最終週次の取得時刻は9/25 16:30 JST以後を要求するよう訂正し、同じstoreで再実行してpassを確認した。
[機械検証結果](./report.md)は最終週次2026-09-18との母集団・単位・残高内訳の連続性を記録している。
契約確認のため同じ日付のClientV2応答を別途照合し、保存行との差異がないことを確認した。

## 反映の境界

この変更はruntime activationとその証拠を含む。merge、本番L1公開とruntime storeのreadbackは別の未完了工程であり、#973はcloseしない。
#1010の実screeningにおける指標充足率検証も未完了である。scaffolding撤去taskはactivationとday-one本番検証の完了後に登録する。
