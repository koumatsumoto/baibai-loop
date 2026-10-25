# 企業成長

`company_growth/` は企業価値と兄弟のL2で、過去の実績状態と変化をResearchへ渡す。将来予測・成長認定・総合点・Screeningの足切りは行わない。

## 入力と時間

初期の全社四半期実績はJ-Quants `fins/summary` を使い、会社予想を実績へ混ぜない。L3でEBITDAの構成・減価償却等を詳しく調べるときはEDINET原典を参照する。EDINET詳細抽出を成長L2の実行条件にせず、既存valuationのEDINET利用は維持する。EDINETの現行metrics/factsはこの四半期全社履歴の代用品とせず、CF・投資・segment等の次の問いや同一期照合に応じてmarket側の小さなadapterを検討する。JQ `fins/details` も追加項目の候補であり、JQ優先を全項目の永久ルールにしない。EDINET docIDをJQ DiscNoへ偽装せず、差異は期間・連結・科目・訂正を確認するまで並存させる。採否の根拠は[Issue #1588の入力元比較](https://github.com/koumatsumoto/baibai-loop/issues/1588)に残す。

公開入口は `read_api.company_growth.company_growth(path, ticker=..., as_of=..., start=..., end=..., mirror=...)`。start/endは出力する**会計期末**の範囲で、endの省略時はas_of。原本の開示日範囲ではない。release全体の `data_as_of` は他datasetを含む最古watermarkなので、財務開示日終端へ流用しない。startの省略は保持入力全体を意味し、保存期間の上限を設けない。

読取は既存の `read_financial_documents(..., ticker=...)` を使い、全保持文書から状態・比較を計算して出力だけを切る。窓より前のTTM/前年比入力と、窓より後に開示された過年度訂正を残す。同一release・方法・比較ペアで共通する期の結果は窓の両端に依存しない。読取時に外部取得は起動しない。

実行用market SQLiteの `lake_store_origin` は更新基底のrelease ID/digestを選ぶ参照で、作業行がそのreleaseに一致する証拠にはならない。読取はローカルlake mirror（既定 `stores`）で既存release readerがmanifest・dataset・Parquetのdigest/schemaを照合した不変objectだけを使う。財務・masterの作業SQLite行を使わず、同releaseの財務ParquetをDuckDBのメモリ内viewから既存 `read_financial_documents` へ渡す。新しいDBは作らない。更新済みSQLiteが旧originを保持していても、新しい未公開行を旧releaseの事実として返さない。mirror未配置・digest不一致等では成長だけを利用不可とし、外部へ自動接続しない。`collection_boundary=fixed_release_membership` は当該manifestへの収録境界を指す。ローカルの取得時刻cutoffを証明しないため `receipt_cutoff` は未提供。Researchの `input_known_at` は選択時の別の時刻で、開示日やreleaseの収録境界へ読み替えない。過去のas_ofを指定しても、当時の受領状態は復元しない。固定releaseがなければ成長を未取得とし、現在評価は妨げない。

store-localのsource coverageはreleaseへ固定されていないため、現段階では取得coverageを常に `unverified` とする。観測された最古/最新会計期・観測期末一覧・観測間の穴は別に返し、文書の存在から連続取得済みとは推定しない。厳格な既存readerのcoverage条件は変更しない。

## 値と変化

- `reported` は原本の公表期間の累計/通期。報告基本EPS、平均株数、営業CFはこの粒度を保つ。
- `quarter` は同じ年度開始・連結基準の実績金額から成立する単独四半期。EPS・CF・株数を差分しない。
- `annual_ttm` は実日数で年間と認められる公表値、または成立する金額の「当期累計＋前期通期－前年同期間」。算術の成立は経済的比較可能性の完全な保証ではない。
- `balance` は観測期末の総資産・純資産・発行済株数・自己株式数。古いBSをコピーせず、純資産を普通株主資本と呼ばない。

指標ごとに採用文書を保持する。部分訂正は該当科目だけへ適用し、同順位の矛盾は該当科目だけを欠測にする。算術の比較元会計期間は科目の値を見る前に選び、科目欠測で別の許容期間へ切り替えない。これにより一つのObservationの開始日を別科目のoperandで上書きしない。比較元累計だけが後日変更され、比較先累計との既知の不整合があるときは無条件に差し引かない。同日の既知時刻による前後は保持し、時刻不明で変更後の対応が確定できない場合は `cumulative_revision_order_unresolved` とする。時刻が不明なだけで変更のない累計を矛盾とはしない。連結/単体や変則決算の不成立も局所化する。営業利益を経常利益で代替しない。NPの親会社帰属は連結基準を確認できる場合だけ提供する。金融業・業種未確認では非金融の営業利益率を提供しない。

`comparison_basis=source_reported_economic_scope_unverified` は、保持した各期の公表値同士の算術であり、継続事業・非継続事業、連結範囲、会計方針や為替を同一基準へ組み替えた成長率ではないことを示す。会社が当期資料に示す組替後の前年比とは異なる場合がある。L3は重要な変化を原典で確認し、本業の成長・一時損益・事業分離を区別する。補正値で保存原値を上書きせず、必要な組替比較と根拠は評価側へ記録する。

変化は両期の実数・差・通常率（比較元が正の場合）・符号変化、利益率のポイント差。正→負を率から落とさず、負/ゼロの比較元は通常率を主表示しない。EPS・株数の原値は残すが、原本間の分割基準が未確認なので、その差/率は提供しない。表示窓の末尾で暗黙の株式換算基準を決めない。

`not_observed`（元値なし）、`*_incomparable` / `inconsistent_cumulative_revision`（合成/比較不能）、補助機能の `not_provided`、coverageの `unverified` を区別する。価格・一部科目の不足で実績全体を消さない。

PERとpeerはこの版では **未提供**。PERは価格・期末自己株式控除後株数・正の年間/TTM親会社利益との単位対応を未検証。報告EPS型や別倍率へ切り替えない。peerは基準日・根拠・銘柄集合を固定した比較群が未選定で、比較ペアごとの両期母数と率母数を検証していない。現在の相対評価や市場全体を代用品にしない。今後のPER/peer追加でも実績状態・採用原本・Changesを変えない。追加時は入力対応を確認する。

## Researchと拡張

mirrorの準備・指定は[Research入力の準備](../../batch/OPERATIONS.md#research-input-preparation)に従う。Selection draftの任意 `company_growth_start` / `company_growth_end` で問いに合う会計期末範囲を選ぶ。省略時は保持済み全範囲を選ぶ。範囲外も含む観測可能範囲を結果内で区別する。固定入力の `securities_context[ticker].company_growth` に計算済み状態・変化・release・方法・要求範囲・不足を完全保存する。再開ではこの結果を投影し、最新原本・新しい計算法へ付け替えない。旧Selectionは保存された入力のまま成長なしで再開できる。InputGapは引き続き `analysis.unavailable` と厳密に一致し、成長欠測は加えない。

通常L3投影は直近12会計期のFY/累計表（売上・営業利益・親会社利益・営業利益率・報告EPS）と同期間の保存済みYoYを軸にする。最新会計期の単独四半期・年間/TTM・BSと保存済みYoY、表示範囲の期間付き営業CFも短く投影する。最新値の欠測を過去の正常値で埋めず、CFを四半期差分・年率化しない。Q1と同期間のreported、重なるTTMは別の独立証拠ではない。12期は[固定した入力量測定](https://github.com/koumatsumoto/baibai-loop/blob/4680cc0c3d33212e9e2779d0eaa38e8babdb8f7c/docs/reference/company-growth.md#2026-10-24-の実データ測定)を根拠にした表示既定で、原本保存期間・query範囲・Selectionの要求範囲を制限しない。投影の `saved_scope` と `display_scope` に保存範囲と表示範囲を分け、欠測理由・文書ID/開示日時・固定release・より長い観測範囲を残す。株式基準は未検証のまま保持する。四半期単独/TTMをFY/累計へ混ぜたり、同じ値を四つのobservationとして反復したりしない。

表示の切替は[Research skill](../../.agents/skills/research/SKILL.md#完了と再開)に従い、同じ保存結果から投影する。表示期数はworkspace manifestだけの設定で、保存入力・selection hash・draftのstart/end・同一draft判定を変えない。`full_result_ref` はOperation内のResearchSelectionを起点としたJSON pointerであり、全状態/Changes・BS/CF・比較元/採用文書を完全結果で確認できる。原本の再取得や財務再計算は行わない。

新しい実績指標の選択・算術はcompany_growthへ、新providerのデコード・文書identity・保存読取はmarket/Collectionへ置く。Research/UIへ算式をコピーしない。共通marketの `financial_periods` は両領域で意味が同じ期間比較・実績矛盾処理・金額算術だけを持つ。valuationの最新anchor・fallback・carry・TTMRulesは移さない。import-linterが兄弟領域、純粋計算、Web/Batch入口の境界を検査する。

一回限りの実データ受入は[Issue #1588](https://github.com/koumatsumoto/baibai-loop/issues/1588) / [draft PR #1592](https://github.com/koumatsumoto/baibai-loop/pull/1592)の未完了作業である。原本値・期間・単位の独立照合、既存入力と薄い表を残すL3対照比較、実際のprompt使用量の確認を残す。測定済みの有限範囲と手順は[固定時点の受入記録](https://github.com/koumatsumoto/baibai-loop/blob/4680cc0c3d33212e9e2779d0eaa38e8babdb8f7c/docs/reference/company-growth.md#実データ受入の前提とci)から辿り、合成CI成功を実データ受入の完了へ読み替えない。
