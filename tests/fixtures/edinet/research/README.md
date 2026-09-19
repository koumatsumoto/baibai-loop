# Research fact source fixtures

EDINET type=1のPublicDoc instance、issuer XSD、Japanese label linkbaseから、対象数値、context、unit、member定義とlabel、借入金・社債TextBlockを抜粋したもの。2026-09-19取得。数値・期間・HTML tableは変更せず、無関係な本文と要素を除き、XSD参照先のbasenameをfixture名へ置換した。テスト時にZIPへ束ねる。

| docID | 会社 | 主な検証 |
| --- | --- | --- |
| S100YJ24 | 1811 錢高組 | 営業利益、比較年度、リースのみの長期満期表、社債該当なし |
| S100YR5P | 1873 日本ハウスホールディングス | 外部売上と内部込み売上、負の利益、借入金・社債、括弧内額の二重計上防止 |
| S100YCUF | 5410 合同製鐵 | 経常利益basis、報告セグメント合計・その他 |
| S100XTM0 | 2501 サッポロホールディングス | IFRS売上・営業利益、nilと調整行 |

原典: [EDINET](https://disclosure2.edinet-fsa.go.jp/) の各docID。有効なAPI認証で `/api/v2/documents/<docID>?type=1` から取得できる。
