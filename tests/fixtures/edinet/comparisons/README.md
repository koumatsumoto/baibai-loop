# IFRS本表の同一文書比較operand

2026-10-31に既存EDINET API v2で取得したtype=1原ZIPから、DEI・本表の当期/比較期context・JPY unit・sales/OP/parent profit fact・対応presentation roleを抜粋した。値・日付・精度・memberは変更していない。JSONは同じdocIDの一覧原本行である。fixture用ZIPのentry名だけをdocIDへ短縮する。抽出器のlocatorは渡されたZIP内のentryと0始まりのinstance直下child位置を指す。

| docID | 提出日時（JST） | 原type=1 ZIP SHA256 | 実証範囲 |
| --- | --- | --- | --- |
| S100YRSR / 436A | 2026-07-24 15:31 | `6c610c6d9ee2eb440470f3794b94aa673afd623089eb97dc86b0ea2d7017b7d0` | IFRS単体年次の売上・OP。親利益は抽出しない |
| S100YV6Y / 1605 | 2026-08-10 13:34 | `7c2911799a7e0f152e82e1ccc68c79671b7fe47f5f1f9f91c6bfa70effd0e988` | IFRS連結半期の売上・OP・親利益 |

原典は[EDINET](https://disclosure2.edinet-fsa.go.jp/)の当該docID。既存[CSV fixture](../README.md)と売上・OPの値/contextを照合した。半期はtype=1 contextの2026-01-01〜06-30と比較期2025-01-01〜06-30を使い、一覧metadataの12-31から期間を作らない。

この確認は年次・半期に限る。Q1/Q3の可用性、J-Quantsとの文書対応、9268 Q1の組替値やTTM復元を実証したものではない。認証付きJ-Quants応答はfixtureへ含めない。
