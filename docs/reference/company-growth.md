# 企業成長

企業財務と並ぶL2モデルとして、過去実績の状態と変化から調査の問いを作る。将来成長の認定・総合点・候補の足切りはしない。算式・field・期間条件は[所有実装](../../engine/src/baibai_engine/machine_analysis/models/company_growth/)が正本である。

## 入力と時間

全社実績はJ-Quantsの財務summaryを使い、会社予想を混ぜない。EDINETの詳細factsはこの履歴の代用品や全社生成の実行条件ではない。重要なCF・事業内訳はL3が原典へ戻る。入力元の比較と拡張候補は[Issue #1588](https://github.com/koumatsumoto/baibai-loop/issues/1588)に残す。

表示範囲は会計期末で、開示日や取得範囲ではない。窓外の比較元と後日訂正を含む保持文書から計算し、表示を絞る。範囲変更で共通する期の意味を変えず、読取から外部取得を起動しない。

通常生成のnative原本集合は[共有固定入力](../../jobs/OPERATIONS.md#financial-native-l1)、既存の個別lake照会は[固定releaseの読取](../../engine/src/baibai_engine/machine_analysis/observations/financials/fixed_financial_reader.py)に従う。保存入力を最新headや作業SQLiteへ読み替えない。

入力への収録境界、開示日cutoff、L3の入力確定時刻は別である。保持原本からの事後の振り返りであり、当時の受領vintageの完全再現や連続取得coverageの証明ではない。固定入力の不足は他領域まで停止させない。

## 実績を読むときの境界

公表累計/通期、成立する単独四半期、年間/TTM、期末残高を区別する。EPS・平均株数・営業CFを四半期差分へ変換せず、同じQ1や重複TTMを独立した成長確認に数えない。

比較期間は値の有無より先に決める。会計基準は連結/単体区分と別に[開示owner](../../engine/src/baibai_engine/machine_analysis/observations/financials/financial_documents.py)が解決する。部分訂正は科目ごとに扱い、既知の競合や基準変更を古い正常値で迂回しない。比較元の欠測を別期間で埋めず、時刻不明だけで変更のない観測を捨てない。

算術が成立しても、事業売却・M&A・連結範囲・会計方針・為替まで同じ経済的範囲へ組み替えた値ではない。J-Quants summaryは過去の組替比較値を遡及供給しない。L3は重要な増減を原典へ戻して本業・一時損益・事業分離に分け、必要な組替比較を評価側へ記録する。保存原値は上書きしない。

赤字縮小・黒字化は元の実額と符号変化で読み、負/ゼロ基準の通常率だけで改善を決めない。営業利益を経常利益で代替せず、金融業へ非金融の営業利益率を当てはめない。EPS・株数は原値を残すが、株式basis未検証の変化率は作らない。

企業成長モデル自体はPER・peer比較を提供しない。未提供機能・元値欠測・比較不能・coverage未検証を区別し、別倍率や現在の市場平均で埋めない。それだけで企業価値の調査を止めない。

### EDINET同一文書の比較

固定入力のtype=1原ZIPにあるIFRS年次・半期の本表から、同一文書の当期・比較期を別系列の`reported_comparisons`へ保存する。原金額・精度・期間・scope・原文位置を保持する[抽出owner](../../engine/src/baibai_engine/machine_analysis/observations/financials/edinet_comparisons.py)が正本である。企業財務も同じ比較を保存するが、J-Quantsの報告値・TTM・価格倍率のoperandへ横結合しない。単体利益を親会社利益へ読み替えない。

受領済み原本と、その訂正系列を文書ごとの履歴として扱う。未取得の訂正を旧文書で埋めず、原本不足はJ-Quants系列と分ける。比較期の日付をCSV相対年度名や書類年度から推定せず、公表前の評価へ使わない。固定objectの破損は欠測へ変換せず、保存済み入力・ID・hashを書き換えない。

[436A年次・INPEX半期のfixture](../../tests/fixtures/edinet/comparisons/README.md)の成立をQ1/Q3や他の本表へ一般化しない。取得制約と本番受入結果は[Issue #1683](https://github.com/koumatsumoto/baibai-loop/issues/1683)に残す。PDFによる比較は別経路で、XBRLの取得を意味しない。

## PDF組替比較の選択

企業財務と同じ[選択owner](../../engine/src/baibai_engine/machine_analysis/observations/financials/pdf_selection.py)が保持開示の最新期・訂正を照合し、同一基準の比較欄とTTMを組み立てる。未受領の訂正まで確認済みとはしない。日本基準の遡及変更には同日公表の組替済み前期実績が必要で、変更前の年度額で埋めない。

表示額由来の近似と端数処理の不明を残し、精密XBRL値と呼ばない。原本hash・文書日時・operandを保持し、旧原観測の基準や金融業/業種未確認の利益率制約を変更しない。取得と受入は[運用手順](../../jobs/OPERATIONS.md#jpx決算pdfの表示額による補完)を参照。

## Investment Judgmentへの接続

保存された成長結果を他モデルと一緒にSelectionへ固定する。長期の問いには[skillの表示・再開手順](../../.agents/skills/investment-judgment/SKILL.md#完了と再開)と完全結果を使う。表示変更は原本保存期間・生成範囲・入力hashを変えず、保存モデル自体の範囲不足も補えない。

旧Selectionは保存された形式・欠測・入力のまま再開する。固定旧入力から別generationを生成・保存する専用CLIは未提供で、現在marketの公開jobを代用しない。[初期の入力量測定](https://github.com/koumatsumoto/baibai-loop/blob/4680cc0c3d33212e9e2779d0eaa38e8babdb8f7c/docs/reference/company-growth.md#2026-10-24-の実データ測定)と[有限受入](https://github.com/koumatsumoto/baibai-loop/issues/1588)は、全市場の原典照合や収益改善の証明ではない。
