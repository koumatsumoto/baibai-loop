# 企業成長

企業財務と並ぶL2モデルとして、過去実績の状態と変化から調査の問いを作る。将来成長の認定・総合点・候補の足切りはしない。算式・field・期間条件は[所有実装](../../engine/src/baibai_engine/machine_analysis/models/company_growth/)が正本である。

## 入力と時間

全社四半期実績はJ-Quantsの財務summaryを使い、会社予想を混ぜない。EDINETの詳細factsはこの履歴の代用品でも実行条件でもなく、重要なCF・事業内訳はL3が原典へ戻る。sourceを跨ぐ比較は文書identity・期間・連結・科目・訂正を揃える。入力元の比較と拡張候補は[Issue #1588](https://github.com/koumatsumoto/baibai-loop/issues/1588)に残す。

表示範囲は会計期末の範囲で、開示日や取得範囲ではない。窓外の比較元と後日訂正を計算から落とさないため、保持文書から状態・比較を作って出力を絞る。対象範囲を狭めても共通する期の意味を変えず、読取から外部取得を起動しない。

個別照会は固定lake releaseの不変objectを読む。作業SQLiteのoriginは更新基底を示すだけで、未公開の作業行がそのreleaseに属する証明ではない。共通モデル生成は公開するreleaseに対応するsealed入力を使う。この二つの入口を同じ作業SQLite読取と説明しない。

releaseへの収録境界、開示日cutoff、L3の入力確定時刻は別である。保持原本からの事後の振り返りであり、当時の受領vintageを完全再現した成長率ではない。観測の最古/最新期や間の穴も、連続取得coverageの証明にはならない。固定入力が利用できなければ成長を未取得とし、他領域まで停止しない。

## 実績を読むときの境界

公表累計/通期、成立する単独四半期、年間/TTM、期末残高を区別する。EPS・平均株数・営業CFを四半期差分へ変換せず、同じQ1や重複TTMを独立した成長確認に数えない。

部分訂正や欠測は科目ごとに扱い、別期間の正常値へ切り替えて比較を成立させない。比較元の期間は値の有無より先に決める。同じ実期間に複数の会計identityが残る場合も原観測を統合せず、同値の科目は比較を保ち、競合する科目だけを比較不能にする。累計訂正の順序が不明な場合と、変更自体がない場合を区別するのは、既知の不整合を差し引かず、時刻欠測だけで有効な観測を失わないためである。

会計基準は連結/単体の`statement_basis`と別に、[J-Quants公式DocType一覧](https://jpx-jquants.com/ja/spec/fin-summary/typeofdocument)の決算短信`DocType`からJP・US・JMIS・IFRSを解決する。前年比較と累計差分・TTMは最新候補と対象期間を固定してから両方の基準を照合する。未知の基準や変更後の組替値が未観測なら、その計算だけを欠測にする。単独の報告額・価格・株数・配当は残す。J-Quants summaryは過去の組替比較値を遡及供給しない。EDINET同一文書の比較は、以下の別の出典付き観測として保存する。

算術が成立しても、事業売却・M&A・連結範囲・会計方針・為替まで同じ経済的範囲へ組み替えた値ではない。L3は重要な増減を原典へ戻して本業・一時損益・事業分離に分け、必要な組替比較を評価側へ記録する。保存原値は上書きしない。

### EDINET同一文書の比較

固定済みtype=1原ZIPと同じrelease/sealの文書一覧から、IFRS年次・半期の本表に明示された売上・営業利益・連結の親会社利益を`reported_comparisons`へ追加する。これは文書ごとの履歴であり、最新値の集約でもJ-Quants系列の補完でもない。当期operandと比較期operandは同じdocIDに属し、原金額文字列・decimals・unit・native期間・連結範囲・本表role・ZIP hash・entry/child locator・提出日時・抽出revisionを保持する。差額・正の前年額に対する率は企業成長ownerが計算し、企業財務も同じ結果を保存する。単体利益を親会社利益へ読み替えない。

比較期の日付をCSV相対年度名や一覧の年度末から作らず、提出日時より前の評価には使用しない。訂正・取下げは既存EDINET操作行の解決に従い、欠けた訂正ZIPを以前の文書から埋めない。全vintageを明示し、原本未配置は比較入力の不足として既存J-Quants観測と分ける。存在する固定objectのhash不一致は従来どおり読取を止める。保存済みpayload/ID/hashは書き換えず、別の原本を既存入力へ付け足したことにはしない。

実証fixtureは[436A年次・INPEX半期](../../tests/fixtures/edinet/comparisons/README.md)。このEDINET経路の可用性をQ1/Q3や他の本表roleへ一般化しない。保存PDFを使う組替比較・TTMは後述の別経路であり、XBRLを取得できたことにはならない。具体的な取得制約と本番受入結果は[Issue #1683](https://github.com/koumatsumoto/baibai-loop/issues/1683)に残す。

赤字縮小・黒字化は元の実額と符号変化で読み、負/ゼロ基準の通常率だけで改善を決めない。営業利益を経常利益で代替せず、金融業へ非金融の営業利益率を当てはめない。EPS・株数は原値を残すが、株式basis未検証の変化率は作らない。

企業成長モデル自体はPER・peer比較を提供しない。価格・利益・資本の単位対応や比較群の未検証を、別倍率や現在の市場平均で埋めない。未提供機能・元値欠測・比較不能・coverage未検証を区別し、それだけで企業価値の調査を止めない。

## PDF組替比較の選択

保存PDFの最新開示照合は企業財務と同じ[選択owner](../../engine/src/baibai_engine/machine_analysis/observations/financials/pdf_selection.py)を使う。選択した同一文書の比較欄は当期/Q1の前年比較へ、年度＋当期−比較期はTTMへ接続する。最新とは保持開示の範囲内であり、未受領の訂正まで確認済みとはしない。表示額由来の近似・原本hash・文書日時を残し、旧原観測の基準を変更しない。金融業/業種未確認の利益率制約は維持する。選択と訂正の運用は[取得・受入手順](../../jobs/OPERATIONS.md#jpx決算pdfの表示額による補完)を参照。

## Investment Judgmentへの接続

保存された成長結果を他モデルと一緒にSelectionへ固定する。表示を広げることと生成入力を増やすことは別で、長期の問いには[skillの表示・再開手順](../../.agents/skills/investment-judgment/SKILL.md#完了と再開)と完全結果を使う。表示変更で入力hashや原観測を変えない。

通常生成は全保持範囲を扱うが、保存モデル自体の範囲不足は表示切替では補えない。固定旧入力から別generationを生成・保存する専用CLIは未提供で、現在marketの公開jobを代用しない。[開始・再開手順](../../.agents/skills/investment-judgment/SKILL.md)に従う。旧Selectionも保存された形式・欠測・入力のまま再開し、新しいgenerationへ読み替えない。

通常表示の期数は原本保存期間や生成範囲の制限ではない。[初期の入力量測定](https://github.com/koumatsumoto/baibai-loop/blob/4680cc0c3d33212e9e2779d0eaa38e8babdb8f7c/docs/reference/company-growth.md#2026-10-24-の実データ測定)と[有限受入の範囲](https://github.com/koumatsumoto/baibai-loop/issues/1588)を、全市場の原典照合や収益改善の証明へ一般化しない。
