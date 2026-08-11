---
title: "支配権イベントの実現 exit 値置換 — 事前登録"
summary: "成立した公開買付けの現金対価で上場廃止銘柄の未解決 forward return を置換し、根拠が確定しない行は既存の両側 bracket に残す契約を固定する。"
doc_type: measurement-record
status: active
date: 2026-08-11
---

# 支配権イベントの実現 exit 値置換 — 事前登録

価値tier: T1 — 上場廃止で観測不能になった長期 return を成立済み公開買付けの現金対価で実値化し、買収プレミアムを全損・中立代入へ落とす較正誤差を減らす。

## 1. 固定する問いと非盲検性

現行 forward builder は target date 以前の最終取引日が15暦日より古い銘柄を `unresolved_stale_exit` とし、authority は未解決行を全損 `-1.0` と同 cohort の resolved 中央値で挟む。既報では満期済み cohort が例外なく上場廃止銘柄を含み、3y 1件・5y 1件で core metric の結論方向が割れた。公開買付け由来の上場廃止では中立代入が実現対価の上限にならないことも既知である。

本書を commit するまで、公開買付価格を forward row へ結合した return、置換件数、bracket 感度、eligible cohort、較正 metric の前後差を計測しない。EDINET / JPX のschema、様式別件数、ticker解決率、資料の取得可能性は outcome-free source coverage として確認してよい。

この変更が答える問いは「満期前に現金対価が確定して市場価格を観測できなくなった銘柄を、その確定対価で較正できるか」である。イベントの予測力、TOB戦略の収益性、candidate annotation の選別力は評価しない。

## 2. source と event identity

一次 source だけを使う。

- JPX 上場廃止銘柄一覧から ticker、上場廃止日、理由を保持する。
- EDINET 書類一覧の `docTypeCode`、`edinetCode`、`issuerEdinetCode`、`subjectEdinetCode`、提出日時、法定・開示・取下げ状態、親書類IDを保持する。
- 公開買付届出書系は `240`（届出）、`250`（訂正）、`260`（取下げ）、`270`（報告）、`280`（訂正報告）とする。大量保有報告書系 `350` / `360`、意見表明報告書系 `290` / `300`、臨時報告書 `180` / `190` はイベント索引には使えるが、exit price の根拠にはしない。
- 対象 ticker は公開買付書類の `subjectEdinetCode` を、同じEDINET一覧履歴で観測した `(edinetCode, secCode)` 対応へ結合して解決する。提出者の `secCode` を対象 ticker とみなさない。対応が一意でない場合は未解決にする。

現行storeは対象EDINET codeを保存していないため、schema移行だけで過去行を推定しない。EDINET list metadata を同じAPIから再取込して列を復元する。これは本文・添付ファイルの全銘柄fetchではなく、既存の日次list取込の再実行である。本文取得は較正対象の上場廃止ticker、および運用上のlonglist / shortlist / 保有銘柄に限定する。

## 3. actual exit の採用規則

`(ticker, tender offer)` ごとに次をすべて満たす場合だけ actual exit を作る。

1. JPX の上場廃止日と対象 ticker が一意に対応する。
2. EDINET の対象 ticker 解決が一意で、公開買付届出書 `240` と公開買付報告書 `270` を同一案件へ対応づけられる。
3. 取下げ `260` がなく、法定・開示・取下げ状態が利用可能である。訂正 `250` / `280` がある場合は提出日時順の最終有効状態を使う。
4. 最終有効な届出書本文から普通株式1株あたりの円建て買付価格を一意に抽出でき、正かつ有限である。複数種類株、株式交換、非現金対価、条件付き価格、対象証券の不一致は採らない。
5. 公開買付報告書が成立を示し、JPX 上場廃止理由と案件が矛盾しない。
6. entry からstore上の最終barまで `adjustment_factor` coverage が complete である。

actual exit の日付はJPX上場廃止日とする。公開買付価格は source 上の1株基準なので、上場廃止日より後からstore最終barまでの `adjustment_factor` を累積して、`asof_basis_closes` と同じ最終株式基準へ換算する。上場廃止後に当該tickerの株式調整eventが存在する、またはcoverageが不完全なら実値化しない。

同じ ticker に複数案件がある場合は、JPX上場廃止日へ対応し、成立報告と価格が一意な案件だけを採る。価格・対象・成立・株式基準のどれかが競合または欠損する場合、値を推定、中央値補完、文面から裁量選択しない。

## 4. forward row の置換規則

actual exit は次をすべて満たす row だけを置換する。

- ticker が一致する。
- `entry_date < delisted_on <= target_date` である。
- entry price が有効である。
- 現行 status が `unresolved_missing_exit` または `unresolved_stale_exit` である。
- §3 の actual exit が有効である。

置換後は `resolved = true`、`status = resolved_control_event_exit`、`exit_date = delisted_on`、`price_return = adjusted_offer_price / entry_close - 1` とする。実現配当は entry date より後、上場廃止日以前の既存 FY actual だけを現行契約で集計する。actual exit の根拠をforward cacheで区別できるtyped fieldを保持し、通常の市場終値resolved行と混同しない。

次は変更しない。

- target date以前15暦日以内に市場終値がある通常resolved行をactual exitで上書きしない。
- actual exit が確定しない行は現行 unresolved status のまま残す。
- authority の全損・中立 bracket は、actual exit で解消しなかった未解決行だけへ適用する。
- panel、universe、valuation、FV、E[r]、gate、selection rank、candidate payloadを変更しない。

forward CSV の語義と列を更新するため calibration cache schema は14から15へ上げ、80 cohortを同じpanel、rules hash、as-of、horizonで再構築する。旧schemaを新語義として読まない。

## 5. 固定する前後比較

比較対象はproduction calibration storeの全80 cohortとし、同じ市場store snapshot、panel、rules hash、as-of、horizon、required metricで次の2系統を生成する。

- baseline: actual exit を無効にした現行 market-close + unresolved bracket契約。
- actual-exit: §3〜§4だけを有効にした契約。

dated report と機械artifactへ次を全件固定する。

- source coverage: JPX上場廃止件数、EDINET様式別件数、対象ticker解決数、本文取得対象数、actual exit採用・棄却件数と理由。
- forward: horizon別の置換行数、ticker数、置換前status、未解決行数、bracket対象数。
- authority: 満期済み3y / 5yのeligible数、blocker内訳、`unpriced_exit_flips_direction` の解消・発生cohort。
- core metric: `recommended_rank_top5`、`recommended_rank_top10`、`er_calibration` のbaseline / actual-exit値と結論方向。
- integrity: actual exit以外のforward row、panel値、E[r]、selection rankがbyte-equivalentであること。
- artifact とreportのSHA-256、market store identity、calibration rules hash、実行command、実行日時。

## 6. 採否と停止条件

これは効果量を選ぶ仮説ではなく観測済み対価へ置き換えるcorrectness変更である。置換件数やeligible増加数を採否閾値にしない。次をすべて満たせば採用する。

1. fixtureで成立・訂正・取下げ・対象不一致・複数価格・非現金対価を区別し、不確定案件をfail closedにできる。
2. actual exit以外のforward rowと全panel、E[r]、rank、gateが変わらない。
3. 80 cohortのbaselineとactual-exitを同一入力から再現し、前後差を機械artifactで説明できる。
4. 実データの採用行がJPXとEDINET一次資料へ一致し、独立検算で価格・ticker・日付・株式基準の不一致がない。
5. actual exitで解消しない行が既存bracketへ残る。

一次資料のtaxonomyで買付価格を一意に抽出できない、対象ticker mappingを復元できない、または株式基準を証明できない場合は実値を作らず、U3を `insufficient` と報告する。結果を見てdocument type、対象期間、価格選択、fallbackを広げない。

## 7. 解釈の限界

実値化できるのは成立した現金公開買付けだけである。倒産、株式交換、合併、スクイーズアウト条件だけがある案件、資料履歴外の上場廃止はbracketに残る。公開買付価格は市場での売却時点や税・手数料を表さず、短期event-driven returnを測らない。candidate annotation の存在は企業品質、将来リターン、買収確率を意味しない。
