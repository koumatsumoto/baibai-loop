# 企業成長

`company_growth/` は企業価値と兄弟のL2で、過去の実績状態と変化をResearchへ渡す。将来予測・成長認定・総合点・Screeningの足切りは行わない。採用仕様は [Issue #1588 r6](https://github.com/koumatsumoto/baibai-loop/issues/1588)。

## 入力と時間

初期の全社四半期実績はr6で採用されたJ-Quants `fins/summary` を使い、会社予想を実績へ混ぜない。ユーザー承認済みの役割分担として、L3でEBITDAの構成・減価償却等を詳しく調べるときはEDINET原典を参照する。EDINET詳細抽出を成長L2の実行条件にせず、既存valuationのEDINET利用は維持する。EDINETの現行metrics/factsはこの四半期全社履歴の代用品とせず、CF・投資・segment等の次の問いや同一期照合に応じてmarket側の小さなadapterを検討する。JQ `fins/details` も追加項目の候補であり、JQ優先を全項目の永久ルールにしない。EDINET docIDをJQ DiscNoへ偽装せず、差異は期間・連結・科目・訂正を確認するまで並存させる。今回新しいprovider基盤や全財務抽出は追加しない。採否の根拠はIssueの「入力元の採否」節が所有する。

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

変化は両期の実数・差・通常率（比較元が正の場合）・符号変化、利益率のポイント差。正→負を率から落とさず、負/ゼロの比較元は通常率を主表示しない。EPS・株数の原値は残すが、原本間の分割基準が未確認なので、その差/率は提供しない。表示窓の末尾で暗黙の株式換算基準を決めない。

`not_observed`（元値なし）、`*_incomparable` / `inconsistent_cumulative_revision`（合成/比較不能）、補助機能の `not_provided`、coverageの `unverified` を区別する。価格・一部科目の不足で実績全体を消さない。

PERとpeerはこの版では **未提供**。PERは価格・期末自己株式控除後株数・正の年間/TTM親会社利益との単位対応を未検証。報告EPS型や別倍率へ切り替えない。peerは基準日・根拠・銘柄集合を固定した比較群が未選定で、比較ペアごとの両期母数と率母数を検証していない。現在の相対評価や市場全体を代用品にしない。今後のPER/peer追加でも実績状態・採用原本・Changesを変えない。実データ受入で入力を確認してから、この中核に追加する。

## Researchと拡張

`research prepare --company-growth-mirror <LOCAL_MIRROR>` で配置済みmirrorを指定できる。Selection draftの任意 `company_growth_start` / `company_growth_end` で問いに合う会計期末範囲を選ぶ。省略時は保持済み全範囲を選ぶが、これは測定済みの推奨入力サイズではない。範囲外も含む観測可能範囲を結果内で区別する。固定入力の `securities_context[ticker].company_growth` に計算済み状態・変化・release・方法・要求範囲・不足を保存する。再開ではこの結果を投影し、最新原本・新しい計算法へ付け替えない。旧Selectionは保存された入力のまま成長なしで再開できる。InputGapは引き続き `analysis.unavailable` と厳密に一致し、成長欠測は加えない。

新しい実績指標の選択・算術はcompany_growthへ、新providerのデコード・文書identity・保存読取はmarket/Collectionへ置く。Research/UIへ算式をコピーしない。共通marketの `financial_periods` は両領域で意味が同じ期間比較・実績矛盾処理・金額算術だけを持つ。valuationの最新anchor・fallback・carry・TTMRulesは移さない。import-linterが兄弟領域、純粋計算、Web/Batch入口の境界を検査する。

## 実データ受入の前提とCI

本番storeの読取/実測は、以下の承認済みCI受入と合成CIを区別して記録する。主担当へ渡す最小試験入力は、固定releaseのID/digestと収録manifest、該当releaseの財務・masterと必要な前期間/訂正を含むParquet objectを配置したローカルmirror、origin参照の読取専用marketコピー、財務原文書と元単位・開示日/時刻・期間・連結基準、取得coverageがreleaseに対応する根拠（なければ未確認）、採用対象一覧である。追加課金や認証変更は不要な既存経路だけを使う。

初期サンプルは公開情報の4〜6社・各8〜12四半期程度とTTM/前年比の必要前期間。保存/参照上限ではない。M&A/訂正、営業利益と親会社損益の乖離、分割、金融業、決算月差を含める。候補は武田・日産・ソニーと金融業・決算月差の各1社。履歴がなければ同じ失敗条件を覆う公開情報の別企業へ置き換える。補助PERには同releaseの原価格日/終値と株式単位/自己株式、peerには明示的な基準日・根拠・固定群・両期財務が追加で必要。

CIは統合draft PRのbranchへpushして既存 `.github/workflows/ci.yml` を起動する。qualityの有限合成回帰 `tests/engine/test_company_growth.py` と既存valuation/Research回帰、format/lint/type/import/drift/securityを対象HEADで確認する。ローカルpytest/lint/typecheck/build/benchmarkを実行しない。実データ受入を実施する際も、承認されたread-only入力を既存CIに渡す手段を主担当が確定してから行い、production取得を合成fixture成功で代行しない。公開repoへ保有・口座・非公開情報や秘密は置かない。

実測では原本値/単位/期間の一致、表示窓不変性、履歴coverageの確認可能範囲、対象企業の再計算時間、L3入力サイズを記録する。対照には既存YoY・株数変化・3FY平均と通常の一次資料調査を残す。代表1〜2例は薄いFY/報告累計表とも比較し、同じ便益なら小さな実装を採用する。便益は仮定/反証の具体化、または同品質での転記・照合負担の低減で測る。L3は「旧入力では曖昧な問い」「成長で区別できた経路と実数」「原典へ戻る未解決点」の3点を比較する。継続成長・有機成長・当時倍率・業界全体の改善・独立証拠数を創作しない。実測済みの範囲は末尾に記録し、原本との独立照合・L3対照比較・推奨参照範囲/入力量の確定は残受入として区別する。

有限サンプルの実行入口は `tools/company_growth_sample.py`。CI上で次のJSONを一時ファイルへ置き、許可された固定releaseの読取専用コピーを渡す。ticker/dateは試験対象であり保存設定ではない。実試験では前述の4〜6社へ展開する。

```json
{
  "input_release": {
    "release_id": "FIXED_RELEASE_ID",
    "manifest_sha256": "FIXED_MANIFEST_SHA256"
  },
  "as_of": "2026-10-24",
  "samples": [
    {"ticker": "4502", "start": "2018-04-01", "end": "2020-03-31"},
    {"ticker": "7201", "start": "2023-04-01", "end": "2025-03-31"},
    {"ticker": "6758", "start": "2023-04-01", "end": "2025-03-31"}
  ]
}
```

```bash
uv run python -m tools.company_growth_sample \
  --sqlite /tmp/market-read-only.sqlite \
  --mirror /tmp/market-mirror \
  --sample-input /tmp/company-growth-sample.json \
  --out /tmp/company-growth-sample-result.json
```

release不一致では中断する。記録は企業別query時間、選択した成長入力のUTF-8 JSON bytes、同じ実績選択から作る薄いFY/累計表のbytesと原数値。bytesをモデルのtoken数や課金額とは呼ばず、L3比較には既存入力・一次資料調査と実際のprompt使用量を別に添える。合成入力によるこの入口の動作は通常CIで検証する。実データ移送は下記の承認済み手動CIでだけ実行する。通常PRのqualityは合成入力のみを使う。実データ出力は一時artifactで扱い、公開repoへcommitしない。

### 承認済みCI実データ受入

本人の承認範囲は、本番R2の企業財務・masterと必要manifestを同repo Actionsへ一時コピーし、検証結果を同じ非公開R2の新規キーへ保存すること。保有・口座・application DBは取得しない。外部providerを再取得せず、本番の既存object・current pointer・原本を変更/削除しない。

PR #1592 のbranchまたはmainで、既存 `ci.yml` の手動入力 `company_growth_acceptance=true` を指定する。quality成功後だけ標準runnerで実行する。R2 credentialは既存の3設定を対象stepだけへ渡し、追加の権限・認証・課金設定を作らない。

```bash
gh workflow run ci.yml --ref feat/1588-company-growth -f company_growth_acceptance=true
```

`tools.company_growth_acceptance` がproduction currentを一度固定し、財務/masterのParquetと必要manifestだけを既存lake cacheへ配置する。4502・7201・6758・8306・9983の5社について保持全期間と直近最大12会計期を読み、既存 `tools.company_growth_sample` で原数値と薄い表を作る。12期は試験窓であって保存/公開queryの上限ではない。8期に満たない対象は不足として返す。表示窓不変性、企業別query時間、12期/全期間/薄い表のJSON bytesを記録し、L3効果の判定は主担当の原本・対照比較に残す。

公開logは件数・時間・bytes・成否と検証result keyだけ。原本・原数値をActions artifactやGitへ出さない。詳細結果は `baibai-stores/validation/company-growth/pr-1592/<run>-<attempt>/<head>/report.json` に `If-None-Match` で新規保存し、receiptと読戻しdigestを照合する。既存キーには書けない。runnerの一時コピーは終了時に削除する。

主担当は公開要約の `private_result_key` を使い、既存の認可されたR2読取環境で結果を一時ファイルへ取得する。秘密・endpointをログへ表示せず、結果ファイルを公開repoへcommitしない。

```bash
AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID}" \
AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY}" \
AWS_DEFAULT_REGION=auto \
aws s3api get-object --bucket baibai-stores --key '<PRIVATE_RESULT_KEY>' \
  --endpoint-url "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com" \
  /tmp/company-growth-private-result.json
```

通常Researchの準備は既存 `hydrate-market` を使う。hydrateは採用した固定releaseのmanifest一式も既存mirrorへ検証付きで保持するため、同じmirrorで後続queryをローカル解決できる。query自体はネットワーク取得を行わない。別配置ならprepareへ `--company-growth-mirror` を渡す。manifest/objectの欠落や読取OSエラーは成長内の `fixed_release_inputs_unavailable` へ局所化する。

### 2026-10-24 の実データ測定

固定code `bc625286631e1e7072d9538f8440d873f763ebee` の [手動CI 40082788945](https://github.com/koumatsumoto/baibai-loop/actions/runs/40082788945) で実施し、qualityと受入jobが成功した。5社とも保持された40会計期を読め、直近12期と全期間の共通状態・採用原本・Changesが一致した。これは確認できた保持範囲であり、保存上限・全訂正の完全性・当時再現を主張しない。

| 銘柄 | 保持期数 / 試験期数 | 全期間query秒 | 12期query秒 | 全期間JSON bytes | 12期JSON bytes | 薄い表bytes |
| --- | --- | --- | --- | --- | --- | --- |
| 4502 | 40 / 12 | 1.578 | 0.210 | 516302 | 159710 | 3746 |
| 7201 | 40 / 12 | 0.233 | 0.235 | 516898 | 159252 | 3754 |
| 6758 | 40 / 12 | 0.271 | 0.223 | 514725 | 159896 | 3766 |
| 8306 | 40 / 12 | 0.227 | 0.225 | 441900 | 135408 | 3390 |
| 9983 | 40 / 12 | 0.230 | 0.228 | 516862 | 159416 | 3746 |

Parquetの一時コピーは22 objects / 39137424 bytes、準備は13.715秒だった。この試験ではquery時間は追加のpartition限定・session再利用を要求する大きさではないため、性能向けの追加機構は見送った。

12期でも約135〜160KB/社で、薄い表は約3.4〜3.8KBだった。bytesの差だけで成長L2の便益を認定せず、通常Selectionでは問いに合う期間を明示する。既存YoY等を残した対照・薄い表とのL3比較、原本値/期間/単位の独立照合、推奨の既定入力量の確定は主担当の残受入とする。

非公開結果は `baibai-stores/validation/company-growth/pr-1592/40082788945-1/bc625286631e1e7072d9538f8440d873f763ebee/report.json`。3302196 bytes、SHA-256 `2dc4fd1c3ae929a2ad76af55e38d7a8a14603dff7849a95c48502c675927f5f3`。条件付き新規保存と読戻しdigest一致を確認し、runnerの一時コピーを削除した。公開log・artifact・Gitへ原本/原数値は出していない。

