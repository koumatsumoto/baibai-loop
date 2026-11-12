# IFRS本表の同一文書比較operand

2026-10-31に既存EDINET API v2で取得したtype=1原ZIPから、DEI・本表の当期/比較期context・JPY unit・sales/OP/parent profit fact・対応presentation roleを抜粋した。値・日付・精度・memberは変更していない。JSONは同じdocIDの一覧原本行である。fixture用ZIPのentry名だけをdocIDへ短縮する。抽出器のlocatorは渡されたZIP内のentryと0始まりのinstance直下child位置を指す。

| docID | 提出日時（JST） | 原type=1 ZIP SHA256 | 実証範囲 |
| --- | --- | --- | --- |
| S100YRSR / 436A | 2026-07-24 15:31 | `6c610c6d9ee2eb440470f3794b94aa673afd623089eb97dc86b0ea2d7017b7d0` | IFRS単体年次の売上・OP。親利益は抽出しない |
| S100YV6Y / 1605 | 2026-08-10 13:34 | `7c2911799a7e0f152e82e1ccc68c79671b7fe47f5f1f9f91c6bfa70effd0e988` | IFRS連結半期の売上・OP・親利益 |

原典は[EDINET](https://disclosure2.edinet-fsa.go.jp/)の当該docID。既存[CSV fixture](../README.md)と売上・OPの値/contextを照合した。半期はtype=1 contextの2026-01-01〜06-30と比較期2025-01-01〜06-30を使い、一覧metadataの12-31から期間を作らない。

この確認は年次・半期に限る。Q1/Q3の可用性、J-Quantsとの文書対応、9268 Q1の組替値やTTM復元を実証したものではない。認証付きJ-Quants応答はfixtureへ含めない。

## 連結年次の追加抜粋

2026-11-12に保持済み原本をhash照合し、連結年次本表の当期/比較期を追加した。S100XU9Lは既存の固定原本bundle、残る2件は固定EDINET inputの原ZIPのみを再読した。取得APIの再要求ではない。XMLは必要なcontext・DEI・unit・factと当該roleのlocだけの抜粋、JSONは公開docID・提出時計・会計期間等の必要項目だけであり、一覧行の完全コピーではない。

| docID / ticker | 提出日時（JST） | 原type=1 ZIP SHA256 | 実証範囲 |
| --- | --- | --- | --- |
| S100XU9L / 1605 | 2026-03-26 15:17 | `e21ee069a6bedc845df4ef6fd37288e736d43e199f95fb5731200e62e06af114` | 2025年次、売上・OP・親利益 |
| S100YG7Z / 6803 | 2026-06-25 13:08 | `b0c8ba935536d64dd82037d63581938e58dd6e9bbe85baefa9e164ddc969094d` | 2026年3月期、売上・OP・親利益。同値の重複factも保持 |
| S100Z3FV / 6030 | 2026-09-24 15:56 | `875a32fb76289d7f9988852489fa7a34aac9af1458a6447e1ed37a9d3f2f4ee9` | 2026年6月期、OP・親利益。Revenue2IFRSは未対応のまま保持 |

3件とも標準`rol_ConsolidatedStatementOfProfitOrLossIFRS`に属する。単体年次roleや包括利益計算書へのfallbackはしない。6030の売上欠測を0やsummary/別conceptで補わず、赤字比較元の成長率も作らない。比較は同一文書のvintageで、J-Quants系列・現在TTM・候補順位の置換ではない。
