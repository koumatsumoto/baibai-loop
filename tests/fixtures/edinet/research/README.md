# Company Research fact source fixtures

EDINET type=1のPublicDoc instance、issuer XSD、Japanese label linkbaseから、対象数値、context、unit、member定義とlabel、借入金・社債TextBlockを抜粋したもの。2026-09-19取得。数値・期間・HTML tableは変更せず、無関係な本文と要素を除き、XSD参照先のbasenameをfixture名へ置換した。テスト時にZIPへ束ねる。

| docID | 会社 | 主な検証 |
| --- | --- | --- |
| S100YJ24 | 1811 錢高組 | 営業利益、比較年度、リースのみの長期満期表、社債該当なし |
| S100YR5P | 1873 日本ハウスホールディングス | 外部売上と内部込み売上、負の利益、借入金・社債、括弧内額の二重計上防止 |
| S100YCUF | 5410 合同製鐵 | 経常利益basis、報告セグメント合計・その他 |
| S100XTM0 | 2501 サッポロホールディングス | IFRS売上・営業利益、nilと調整行 |

表の銘柄とdocIDは出典の組み合わせ。抜粋に一覧の提出時刻は含まれないため、factの提出時刻には固定の試験時計を使う。同一銘柄の別書類を仮定する保存・統合テストだけは、銘柄も明示的に置き換える。

原典: [EDINET](https://disclosure2.edinet-fsa.go.jp/) の各docID。有効なAPI認証で `/api/v2/documents/<docID>?type=1` から取得できる。

### #2692 帰属判定の設計対照（通常抽出・公開には未接続）

[純関数](../../../../tools/experiments/edinet_research_dimensions.py)は[金融庁2025年版](https://www.fsa.go.jp/search/20241112.html)・[2026年版](https://www.fsa.go.jp/search/20251111.html)のXSD・定義リンク・日本語標準ラベルと、上記4試料にある8数値概念・2連結明細表だけを対象とする。資料の提出日ではなくQNameのURIを照合する。公式ZIPのSHA-256は順に `70abacbac7b9d897dc794faadb4a2be46775f618ab79a8888622847a4d62ae90`、`8d57f84802b20a8dc6a517ad6d4768270c294aa6991621d66cb5adb562df5de2`。資料の再取得はテストに不要。

4試料のv2対照はセグメント142行・満期24行。提出者自身の事業ラベルは保持し、公式標準ラベルは製品別名と区別する（例：`ReconcilingItemsMember` の標準ラベルは「調整項目」、v2表示は「調整額」）。試料には提出者の定義リンクがない。標準事業セグメント軸上の独自QNameは提出者が記した組であって、所属を別途検証した証拠ではない。[金融庁ガイド5-4-5-1](https://www.fsa.go.jp/search/20251111/2b-1_InstanceGuide.pdf#page=49)に従い、軸省略だけでは連結としない。裏付けた連結セグメント注記がないときはメンバーを残し、帰属は不明。2つの連結明細表では表の標準概念自身が根拠となる。正規の個別財務諸表向け明細表の新規対応、他年度、義務のない項目への既定適用はこの試験の範囲外。

[報告書インスタンスガイド §5-4-3, §5-4-5](https://www.fsa.go.jp/search/20251111/2b-1_InstanceGuide.pdf#page=45)では `entity/segment` を使わず `context/scenario` にメンバーを置く。試料の明示指定66個は全てscenario直下。実験は平坦化前のcontext IDと配置を照合し、誤配置を「軸なし」に読み替えない。

[提出者別タクソノミガイド 図表5-2-20・21](https://www.fsa.go.jp/search/20251111/2a-1_TaxonomyGuide.pdf#page=90)では軸とメンバーで型・代替グループが異なる。実験は提出者宣言の `type` と `substitutionGroup` をその要素で有効なURIで照合する。元の4つのXSD抜粋は `nonnum:domainItemType` のprefix宣言が欠落しているため、独自60行のQNameと原ラベルは保つが型は未確定（それ以外の82行は標準メンバー）。この抜粋の制約は元提出書類の欠陥を意味しない。別prefixで正規型を明示した合成例のみを型確認の正例とし、軸への差し替え・別URIの同名型は採らない。完全なスキーマ検証や提出者DTSの所属判定ではない。

この対照はv2・固定release・L3・日次を変更しない。読取先には接続しない。v3の保存・読取と新旧世代の分離・採用可否は[設計Issue #2692](https://github.com/koumatsumoto/baibai-loop/issues/2692)の別段階。

### 注記は数値の出現位置に結び付ける

**固定原本での確認（自動テストには含めない）。** NCD `S100YEMH` の固定一覧と保存済みtype=1（SHA-256 `45b0c26da66b2a7ea5359c60b3de2ef2db631010f332b0d8a0ea77de7b08dd8b`）で照合した。v2のセグメント42行はすべて `0105110` の連結セグメント注記内の1出現に一致する。一方、事業セグメント軸のない別の4組は、概念QName・context・金額・単位・精度まで同じなのに、注記内と注記外（`0105020`）の両方に現れる。4組はv2の42行に含まれず、この原本でv2の誤分類を発見したわけではない。

**合成対照。** [テスト](../../../tools/test_edinet_research_dimensions.py)は架空の金額で4組を作り、別ファイルに分けた場合と、同一ファイルの兄弟要素に置いた場合を検証する。同じファイルに正規の注記があっても、隣の区画の数値には適用しない。固定ZIP内のファイル名＋要素の位置を指定し、その位置の数値QName・contextと包んでいる注記の完全QNameを照合する。値から位置を探さない。軸がなければ両方ともセグメント対象外。感度確認のため正規のセグメント軸を仮に与えた場合にも、連結の根拠になるのは注記内の出現だけである。この試験は原本の自動再現でも汎用Inline XBRL readerでもない。正式受入には、候補の原値／nil・単位・精度も同じ固定原本で照合し、出現位置と親注記を特定できなければ注記からの連結判定を保留する。[#2692](https://github.com/koumatsumoto/baibai-loop/issues/2692)の3件は解除しない。
