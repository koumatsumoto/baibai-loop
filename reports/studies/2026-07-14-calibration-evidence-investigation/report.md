# 3y / 5y production evidence の blocked 解消可能性調査（#400）

## 0. 目的と authority 維持

本調査の目的は、estimate calibration の 3y / 5y production decision evidence を block する
data integrity 不足を計数し、forward-only と historical backfill のどちらが効果と複雑性の均衡を
満たすかを判断することである。

本調査は provider 実装、panel schema、screening rules、horizon authority を変更しない。
[`estimate-calibration`](../../../docs/reference/estimate-calibration.md) が定める次の条件を維持する。

- production decision は 3y と 5y の双方を要求する。
- cohort as-of の master snapshot は `exact_date` を要求する。
- survivorship、delisting、corporate-action event coverage は `complete` を要求する。
- input range clamp、candidate partition 不完全、unresolved forward row を許容しない。
- 15 日超の stale exit を resolved return に含めない。

この gate は coverage 不足を改善効果へ誤変換しないために存在する。本調査の出力だけで
production change を許可しない。

## 1. 入力、provenance、実行コマンド

### 1.1 入力と provenance

| 入力 | 値 | 用途 |
| --- | --- | --- |
| repository commit | `9a6edc3bdfa693a1576d45348d99feb46c9c5dbb` | code / contract の基準 |
| rules | `records/_config/screening-rules/2026-07-06T000000+0900.yaml` | panel build |
| rules SHA-256 | `bd4bba8ceb74d08fefb4b670ddf28f2e19b8368679678237599be453f214c103` | rules provenance |
| SQLite | `stores/market/market.sqlite` | master / bars / financial summaries |
| SQLite size | `684,216,320 bytes` | 調査時 inventory。immutable identity ではない |
| SQLite mtime | `2026-07-12 19:44:26 +0900` | 調査時 inventory。immutable identity ではない |
| isolated calibration store | `/tmp/baibai-loop-issue400-calibration-20260714` | 残存 artifact。panel / forward 各46件を再確認 |
| isolated evaluation | `/tmp/baibai-loop-issue400-eval-production.yaml` | 3y / 5y authority evaluation |
| evaluation SHA-256 | `356e09f3c00c0b76a9f99bf78d794e0a1a265eb52e384f975ae56772091832a0` | evaluation provenance |

監査対象の`src/baibai_loop/screening/`、market SQLite schema、estimate-calibration reference、rulesには
上記commitからのworking-tree差分がない。共有worktreeの他領域にある変更は本調査の入力に含めない。

入力 SQLite は size / mtime しか記録せず immutable copy またはlogical digestを固定していないうえ、
調査後に更新済みである。このため strict な再現実行はできない。残存artifactから再確認できたのは、
panel / forward各46件、forward rows 37,670件、raw `resolved` 127件、evaluation SHA-256だけである。
後述のsource inventory、stale 556件、coverage gap等は調査時集計であり、同じ入力に対する厳密再現は
できない。次回は入力SQLiteのimmutable local copyまたはtable単位logical digest、実行した全SQL、
code/rules hashを結果計測前に固定する。

### 1.2 実行コマンド

SQLite は URI の `mode=ro` で読む。

```bash
sqlite3 -header -column 'file:stores/market/market.sqlite?mode=ro' \
  "SELECT COUNT(*) AS rows,
          COUNT(DISTINCT snapshot_date) AS snapshot_dates,
          MIN(snapshot_date) AS min_date,
          MAX(snapshot_date) AS max_date,
          COUNT(DISTINCT ticker) AS tickers
     FROM jquants_master_snapshots;"

sqlite3 -header -column 'file:stores/market/market.sqlite?mode=ro' \
  "SELECT COUNT(*) AS rows,
          COUNT(DISTINCT traded_at) AS trade_dates,
          MIN(traded_at) AS min_date,
          MAX(traded_at) AS max_date,
          COUNT(DISTINCT ticker) AS tickers
     FROM jquants_daily_bars;"

sqlite3 -header -column 'file:stores/market/market.sqlite?mode=ro' \
  "SELECT COUNT(*) AS rows,
          COUNT(DISTINCT disclosed_at) AS disclosure_dates,
          MIN(disclosed_at) AS min_date,
          MAX(disclosed_at) AS max_date,
          COUNT(DISTINCT ticker) AS tickers
     FROM jquants_fin_summaries;"
```

既存 cache の互換性を確認する。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-screening calibration-evaluate \
  --calibration-dir stores/screening/calibration \
  --horizon 3y \
  --horizon 5y \
  --out /tmp/baibai-loop-issue400-old-cache-eval.yaml
```

現行 schema v2 を isolated store へ再構築する。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-screening calibration-build \
  --start 2022-09-01 \
  --end 2026-06-30 \
  --sqlite-path stores/market/market.sqlite \
  --calibration-dir /tmp/baibai-loop-issue400-calibration-20260714 \
  --force

UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-screening calibration-evaluate \
  --calibration-dir /tmp/baibai-loop-issue400-calibration-20260714 \
  --horizon 3y \
  --horizon 5y \
  --run-purpose production_decision \
  --required-asof 2022-09-30 \
  --required-metric recommended_rank_top5 \
  --required-metric recommended_rank_top10 \
  --required-metric er_calibration \
  --out /tmp/baibai-loop-issue400-eval-production.yaml
```

## 2. Current inventory

### 2.1 SQLite source inventory

| source | rows | distinct dates | covered range | distinct tickers |
| --- | ---: | ---: | --- | ---: |
| `jquants_master_snapshots` | 4,443 | 1 | 2026-05-13 | 4,443 |
| `jquants_daily_bars` | 5,210,578 | 1,209 | 2021-08-02〜2026-07-10 | 4,997 |
| `jquants_fin_summaries` | 89,991 | 1,216 | 2021-08-02〜2026-07-10 | 4,362 |

daily bars の `adjustment_factor` は `5,210,578 / 5,210,578 = 100.0%` non-null である。
これは価格 basis の正規化に使えるが、event の種類、効力日、対価を説明する
corporate-action event coverage ではない。

調査時集計ではlatest bar date から 15 日超更新されない ticker は 556 件あった。この集合は上場廃止候補の探索に
使えるが、休止、取得漏れ、商品種別を含み得るため、delisting と同一視しない。
入力SQLiteをimmutableに固定していないため、この556件はstrictに再現確認できた値ではない。

### 2.2 Source coverage gaps

`source_coverage` の成功range間には次の calendar-day gap がある。

| gap start | gap end | calendar days |
| --- | --- | ---: |
| 2023-01-27 | 2023-01-30 | 4 |
| 2025-02-13 | 2025-02-26 | 14 |
| 2025-12-03 | 2025-12-12 | 10 |
| 2026-01-13 | 2026-02-09 | 28 |
| 2026-04-13 | 2026-04-24 | 12 |

calendar-day gap は全日が取引日であることを意味しない。backfill pilot は market calendar と
突き合わせ、必要取引日に row が存在することを別に証明する。

### 2.3 Existing calibration cache

`stores/screening/calibration` は panel / forward を各46件持つが、現行 reader が要求する
`cache_schema_version: 2` を持たない。通常評価は次で停止する。

```text
calibration cache version is missing; run calibration-build --force
```

この cache の metric を current authority evidence として使わない。

## 3. 現行 schema v2 rebuild 結果

残存isolated artifactは panel 46件、forward 46件、全 horizon 合計の forward row 37,670件、
raw `resolved` 127件を持つ。これらとevaluation hashは再確認済みである。

3y / 5y の authority 対象だけを集計すると次になる。

| horizon | cohorts | metric resolved cohorts | master unavailable | master prior | master exact | candidate metric resolved rows | unresolved rows |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 3y | 46 | 0 | 44 | 2 | 0 | 0 | 7,488 |
| 5y | 46 | 0 | 44 | 2 | 0 | 0 | 7,488 |

2026-05-13 の1 snapshotだけが存在するため、2022-09〜2026-04の44 cohortは
`master_snapshot_status: unavailable`、2026-05と2026-06の2 cohortは`prior_snapshot`になる。
monthly cohort as-of と一致する snapshot は0件である。

production evaluation は次を返す。

```yaml
authority: production_decision_evidence
evidence_status: blocked
production_change_allowed: false
```

raw forwardの`resolved`とproduction metricのresolvedを混同しない。3yにはbenchmark forwardとして
resolved 10 rowsがあるが、candidate metricのresolvedは0である。benchmark rowだけでは
`recommended_rank_top5 / top10`または`er_calibration`のproduction evidenceにならない。

実行例の`--required-asof 2022-09-30`は2026-07-14時点で5y horizonが満期前である。このas-ofの
5y未解決をdata integrity不足と数えてはならない。productionの`required-asof`には評価日時点で
horizonが満期済みのcohortだけを指定する。

3y / 5y の共通 blocker は次である。

- `master_snapshot:unavailable` または `master_snapshot:prior_snapshot`
- `survivorship_coverage_status:unknown` または `not_assessed`
- `delisting_coverage_status:unknown` または `not_assessed`
- `corporate_action_event_coverage_status:unknown` または `not_assessed`
- `metric_unresolved`
- available masterを持つcohortでは`unresolved_forward_rows`

[`calibration/cli.py`](../../../engine/src/baibai_engine/screening/calibration/cli.py) は3y / 5yについて
`exact_date`、3 coverageの`complete`、unclamped input、complete partition、resolved forwardを要求する。
[`calibration/forward.py`](../../../engine/src/baibai_engine/screening/calibration/forward.py) は現状、3 coverageを
`not_assessed`で生成し、missing / stale exitだけを`delisting_coverage_status: unknown`へ上げる。

空panelでは入力窓を読まないため、今回の`input_range_clamped: false`は履歴十分性を証明しない。
master backfill後にbars 1,200暦日、financial summaries 730暦日の入力窓を改めて評価する。

## 4. Root cause: master snapshot が履歴として残らない

DB schema は`PRIMARY KEY (snapshot_date, ticker)`を持ち、複数snapshotを表現できる。
[`sqlite_reader.read_eq_master_asof`](../../../engine/src/baibai_engine/screening/sqlite_reader.py) も as-of 以下の最新
snapshotを読み、日付一致を`exact_date`、不一致を`prior_snapshot`として区別する。

一方、[`store_jquants_master`](../../../engine/src/baibai_engine/screening/sqlite_cache/jquants.py) は保存前に次を実行する。

```sql
DELETE FROM jquants_master_snapshots;
```

同時に既存source coverageを削除し、`coverage_key: latest`だけを記録する。
[`JQuantsProvider.get_eq_master`](../../../engine/src/baibai_engine/screening/providers/jquants.py) も基準日を受け取らず、
latest masterを取得する。

したがって、現行の`bootstrap-cache --asof`を反復してもsnapshot historyは増えない。
append-only保存とexact monthly captureを実装するとmaster componentの待機期間だけが始まる。

exact as-of取得の実装判断は、J-Quants公式API仕様を主根拠にし、公式Python clientは実装補助として
照合する。調査時のclientは`jquants-api-client 2.3.0`、repository commitは
`9a6edc3bdfa693a1576d45348d99feb46c9c5dbb`、checked_atは`2026-07-14`である。
follow-up Aでは仕様revision / URL、client version、repository commit、checked_atをfixture provenanceへ
固定し、`date`指定がexact requested as-ofを意味することをlive responseでも確認する。

- 公式J-Quants API:
  https://jpx-jquants.com/en
- 公式上場銘柄一覧API仕様:
  https://jpx-jquants.com/ja/spec/eq-master
- 公式client source（補助照合）:
  https://github.com/J-Quants/jquants-api-client-python/blob/main/jquantsapi/client_v2.py

## 5. 公式sourceと費用比較

| coverage | source | 取得範囲 | 形式 | 費用区分 | 判断 |
| --- | --- | --- | --- | --- | --- |
| historical membership | J-Quants V2 `equities/master?date=` | planのhistory内 | API、Light以上はCSVも利用可能 | Free: 直近12週を除く2年、Light: 5年・1,650円/月、Standard: 10年・3,300円/月、Premium: 最長20年・16,500円/月。税込 | exact monthly snapshotの第一候補。Standardの履歴下限余裕が小さいためB0で実取得範囲を確認し、Premium費用も比較 |
| membership changes | JPX統計月報 17「異動会社・異動銘柄等一覧」 | 公開archiveは2016年以降 | 月次PDF | 無料 | baselineからの再構成とJ-Quants masterの照合に使える。訂正追随とPDF parserが必要 |
| delisting | JPX「上場廃止銘柄一覧」 | 過去11年 | HTML + archive | 無料 | 廃止日、コード、市場、理由を取得できる。TOB、合併、維持基準不適合等を区別できる |
| split / consolidation / ex-rights | JPX統計月報 18「新株落・権利落等一覧」 | 公開archiveは2016年以降 | 月次PDF | 無料 | 権利落日、確定日、分割・併合比率を取得できる。全event completenessの定義が必要 |
| comprehensive corporate action | J-Quants Pro Corporate Action | 配当は2013-02-20以降、その他は2015-05-08以降 | API / SFTP CSV / Snowflake | 法人限定。単一法人300,000円/月、系列法人450,000円/月、税別 | 10年以上・全上場会社を機械取得できる。coverageは強いが費用が大きい |
| JPX Corporate Action Data Service web | 通常eventは過去2年、属性変更は過去10年 | Web検索 / CSV | 一般利用者「その他」の日本語web単一法人90,000円/月、税別 | 通常eventの2年制限により単独backfill sourceには不足 |

一次source:

- J-Quants plan、history、データ種別:
  https://jpx-jquants.com/en
- J-Quants V2 client `get_eq_master(date=...)`（v2.3.0 / `7db90f61d1e5fe97ddc6cfcd5900f07bf4743af1`）:
  https://github.com/J-Quants/jquants-api-client-python/blob/7db90f61d1e5fe97ddc6cfcd5900f07bf4743af1/jquantsapi/client_v2.py#L293-L307
- JPX統計月報 17 / 18:
  https://www.jpx.co.jp/markets/statistics-equities/monthly/index.html
- JPX上場廃止銘柄一覧:
  https://www.jpx.co.jp/listing/stocks/delisted/index.html
- JPX Corporate Action Data Service:
  https://www.jpx.co.jp/markets/paid-info-listing/corporate-action/index.html
- J-Quants Pro Corporate Actionの範囲と価格:
  https://www.jpx.co.jp/english/corporate/news/news-releases/6020/20250722-01.html
- J-Quants Pro料金表:
  https://pro.jpx-jquants.com/pdfs/appendix-1-2-pricing-and-usage-table-en.pdf

daily barsのadjustment factorだけでは、合併、株式交換、現金対価の上場廃止、コード変更を含む
event completenessを説明できない。factor completenessとevent coverageを別fieldとして維持する。

## 6. Forward-only と backfill

### 6.1 Forward-only

最初のexact monthly snapshotを2026-07-31に保存した場合、master componentの時計だけが開始する。
master append-only化はsurvivorship、delisting、corporate-action event coverageを改善せず、これらは
`not_assessed`のままである。別実装によってmembership event、delisting、corporate action、exit
resolutionを2026-07-31から欠損なく維持できた場合に限り、target到達日は次になる。

| evidence | 最短target |
| --- | --- |
| 3y | 2029-07-31 |
| 5y | 2031-07-31 |
| 3y / 5y双方を要求するproduction authority | 2031-07-31以降 |

2031年到達は、masterの月末exact captureに加え、event coverageとexit resolutionを別実装し、
2026-07-31から5年間欠損なく継続できる場合だけの条件付き見積りである。現在のsingle-snapshot置換、
またはevent coverage `not_assessed`を維持する場合、成立時期は到来しない。

### 6.2 Historical backfill

2026-06-30までに満期を迎える最新5y cohortは2021-06-30である。このcohortを現行metricと
同じ入力で再構築するには次が必要になる。

| input | required start / as-of |
| --- | --- |
| daily bars | 2018-03-18（as-ofの1,200日前） |
| financial summaries | 2019-07-01（as-ofの730日前） |
| master | 2021-06-30 exact snapshot |
| forward prices / events | entryから2026-06-30までcomplete |

Standardの10年履歴から2019-10月末cohortを作る場合、1,200日前の必要bars開始日は
2016-07-18であり、2026-07-14時点の10年下限に対して約4日しか余裕がない。取得可能範囲は日々
移動するため、2019-10〜2021-06の21 cohortを固定した大規模backfill案は採用しない。

まずB0 source feasibilityとして、取得開始時点の実際のmaster / bars / financial summaries / event
source下限からmatured month-end cohortを可変に導出する。無料またはStandard sourceの消失が迫り、
人間が承認する場合は、実装完了を待たずauthorized responseをlocal-only immutable rawとして直ちに
captureする選択肢も示す。履歴余裕を買う個人向けPremiumと、machine-readable event coverageを得る
法人向けJ-Quants Pro Corporate Actionは別の選択肢として費用判断する。Premiumだけではevent
completeness問題は解消しない。

backfill実装へ進む場合は次の複雑性を持つ。

- 可変month-endごとのexact master取得と件数・hash検証
- bars / financial summariesの必要窓とsource gapの解消
- JPX月次PDF parser、訂正版の再取得、provenance
- listing、delisting、code change、market transferのpoint-in-time再構成
- TOB、合併、株式交換、現金対価等のexit resolution
- adjustment factorとevent ledgerの照合
- unresolved eventを`complete`へ昇格させないcoverage validator

低いデータ費用で調査する経路はJ-Quants Standardのhistorical master / bars / financial summariesと、
JPX無料月報・上場廃止一覧の組合せである。J-Quants Premiumを含む履歴費用を人間が許容するかも
B0で比較する。J-Quants Proはmachine-readable coverageを得やすいが、法人限定かつ高額である。

## 7. 効果と複雑性の均衡判定

Issue #400の判定は、master componentのforward clockを開始するAと、backfillを実装せずsource
feasibilityだけを確認するB0へ分けて調査をcloseすることである。authorityは緩和しない。

| 選択肢 | 判定 | 根拠 |
| --- | --- | --- |
| (a) 補完実装 | Aのみ採用 | exact masterをappend-only保存しmaster componentのforward clockを開始する。event coverageは別課題 |
| (b) forward蓄積のみ | 主経路として非採用 | append-only化後もproduction authorityまで約5年を要し、#380 / #382等の再評価を2031年まで行えない |
| (c) 3y / 5y要件の再設計 | 非採用 | 現在のblockはsourceとstorageの不足で説明でき、authorityを緩和する根拠にならない |
| historical backfill | B0調査のみ | Standard履歴下限の余裕が小さく、固定21 cohortやparser/toolingを先に実装する根拠がない |

補完sourceの完全性は未検証である。Aの採用はmaster componentだけの改善で、production evidenceの
成立を意味しない。B0がsource availabilityと費用対効果を証明できない場合はauthorityをblockedのまま
維持する。本reportは追加調査の境界を定めたことでIssue #400をcloseできるが、production changeを
許可しない。

## 8. 推奨 follow-up Issues

### A. Master append-only + exact monthly capture

公式API仕様を主根拠に、exact `requested_asof`をprovider、cache key、SQLite snapshot、coverageへ
end-to-endで固定する。既存snapshotを削除せずsnapshot単位でappend / upsertし、同一as-ofの良品を
再実行しても不変にする。

受入条件は次とする。

- exact `requested_asof`を要求・保存し、providerが別日を返したら停止する。
- 異なる2 snapshotを保存した後も各as-ofのcoverageがpassし、as-of別negative testが他snapshotへの
  fallbackを拒否する。
- empty、rejected row、unknown date、異常に小さいpopulationをfail-closedにする。現行契約で意図的に
  除外する非commonな5桁codeは許容し、`raw = persisted + intentional excluded`を要求する。
- 同日良品の再取得は行・hash・coverageを変えない。
- `source_coverage` schemaは増やさず、persisted count / status / rangeだけを永続化する。raw / excluded /
  rejectedはtransaction前の検証logとtest assertionで監査する。
- fixture provenanceに公式API仕様URL/revision、client version、repository commit、checked_atを残す。

この変更が開始するのはmaster componentの時計だけで、survivorship / delisting / corporate-action
coverageを`complete`へ昇格させない。

### B0. Historical source feasibility

parser、tooling、固定21 cohort backfillは実装しない。authorized accessで各sourceの実取得可能な
最古日、訂正・欠損、format、費用を少数sampleで確認し、bars由来の`month_end_asof_grid`をcohort正本に
する。market calendarはbars gridを置換せずgap auditだけに使う。

B0は、開始時点のhistory下限からmatured cohort数を再計算し、Standard / Premium / Pro / JPX無料source、
または人間承認による即時local-only immutable raw captureを比較する。source feasibilityが確認できた後に
だけ、parser、event ledger、isolated backfillを別Issueとして計画する。代表eventのexit valueに1件でも
unresolvedが残る場合、無料sourceのhistorical backfillは非採用とし、高コスト実装へ進まない。

## 9. Limitations

- 本調査はJ-Quants APIへhistorical requestを送らず、公式仕様と現行SQLiteから取得可能性を評価する。
- input SQLiteのimmutable copy / logical digestを固定していないため、inventory、stale 556件、gap集計は
  strictに再現できない。confirmed artifact値と当時集計を区別して読む。
- 現在契約中のJ-Quants planは確認しない。Standard相当の履歴へ実際にアクセスできるかは人間の
  契約状態とAPI responseで確認する。
- JPX月次PDFの全archiveをparseしていない。訂正版、format drift、event分類の網羅性はpilotで測る。
- 556 stale tickerはdelisting件数ではない。
- source coverage gapはcalendar-day rangeであり、missing trading-day件数ではない。
- adjustment factor 100%はcorporate-action event coverage completeを意味しない。
- cohort数は固定しない。B0開始時のbars由来month-end grid、source下限、maturity、unresolved eventから
  可変に導出する。
- overlapping long-horizon cohortから有意性、統計的優位、track recordを主張しない。
- 本reportはprovider、panel、rules、authorityを変更せず、production changeを許可しない。
