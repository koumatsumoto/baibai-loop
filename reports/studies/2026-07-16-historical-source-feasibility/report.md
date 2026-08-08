# Historical source backfill feasibility 限定監査（#420）

価値tier: T2 — 取得不能・不完全なhistorical sourceを完全なas-ofデータと誤認し、screening較正のproduction変更へ使うことを防ぐ。

## 0. 判定

```yaml
decision: blocked
reason: authorization_or_plan_unconfirmed
checked_at: 2026-07-16T07:27:00+09:00
api_raw_requests: 0
local_raw_captures: 0
production_change_allowed: false
```

現在契約中のJ-Quants plan、historical endpointへの実アクセス範囲、raw responseのlocal-only保存許可、
保持・削除方針が人間から確認されていない。Issueの停止条件に従い、J-Quants API requestとraw保存を
行わず、公開一次sourceと既存SQLiteのread-only inventoryだけを確認した。

この状態では`adopt for B1`と`source non-adoption`のどちらも立証できない。前者に必要な取得可能cohort、
source completeness、exit value resolved率が未計測であり、後者に必要な代表eventのunresolved確認も
実施していないためである。判定は`blocked`に固定し、parser、event ledger、backfill、authority変更を
開始しない。

## 1. 入力と権限境界

| 項目 | 監査時点の状態 | 判断への影響 |
| --- | --- | --- |
| repository commit | `ded53056be0bfedddb5ab3a6d22ddaa7a2a1c3ef` | #419 merge後のcontract |
| production rules | `records/_config/screening-rules/2026-07-06T000000+0900.yaml` | rules変更なし |
| rules SHA-256 | `bd4bba8ceb74d08fefb4b670ddf28f2e19b8368679678237599be453f214c103` | input provenance |
| 現在契約中のplan | 未確認 | remote history lower boundを確定できない |
| Standard / Premium相当履歴へのaccess | 未確認 | 取得可能cohortを確定できない |
| raw capture許可 | 未確認 | API responseでfield/date境界を検証しない |
| raw保存path / retention / deletion | 未承認 | `.cache/investigations/420/`は作成しない |
| plan購入・変更 | 未承認 | 費用を発生させない |
| production SQLite | read-only inventoryのみ | row追加・coverage更新なし |

API key、認証header、raw responseはreport、Git、Issue、PRへ記録していない。raw artifactがないため
content hash、response range、row count、取得時刻も存在しない。

## 2. 公開一次sourceで確認できた範囲

公開ページは2026-07-16にlive確認した。公開planの仕様と、このaccountで利用可能なplanは別である。

### 2.1 J-Quants

- [J-Quants plan比較](https://jpx-jquants.com/en)と
  [データ仕様](https://jpx-jquants.com/ja/spec/data-spec)は、公開上の取得可能過去データを
  Free=`直近12週間を除く2年間`、Light=`5年間`、Standard=`10年間`、Premium=`20年間（最長）`
  としている。月額表示は税込でFree 0円、Light 1,650円、Standard 3,300円、Premium 16,500円。
  公開表だけでは現在accountのplanと、個別endpointの実response下限は分からない。
- [上場銘柄一覧API仕様](https://jpx-jquants.com/ja/spec/eq-master)は`date`を
  `YYYYMMDD`または`YYYY-MM-DD`で受ける。休業日を指定すると翌営業日の`Date`を返し、Premiumの
  提供開始日2008-05-07より前を指定しても2008-05-07時点を返す。requested dateとresponse `Date`の
  exact一致はAPI成功だけでは保証されないため、#419のfail-closed検証を維持する。
- 公開FAQは分析結果・分析手法の公開を認める一方、取得データそのものを閲覧可能な形で配布・共有する
  ことを禁止している。local rawの保存期間・削除条件をこの記載だけから確定せず、人間の契約確認を
  必須のままにする。

### 2.2 JPX

- [統計月報index](https://www.jpx.co.jp/markets/statistics-equities/monthly/index.html)は2016年以降の
  年別back number、訂正情報への導線、訂正済みPDFの表示規約を持つ。
  [2016年archive](https://www.jpx.co.jp/markets/statistics-equities/monthly/00-archives-10.html)と
  [2018年archive](https://www.jpx.co.jp/markets/statistics-equities/monthly/00-archives-08.html)では
  `異動会社・異動銘柄等一覧`が資料17、`新株落・権利落等一覧`が資料18である。
- 2026年のcurrent indexでは`異動会社・異動銘柄等一覧`が資料16、`新株落・権利落等一覧`が資料17で、
  Issue仮説の固定番号17 / 18とは一致しない。source番号を期間横断の識別子にせず、各年indexの資料名と
  URLで解決する必要がある。この番号driftだけでsource non-adoptionとは判定しない。
- [上場廃止銘柄一覧](https://www.jpx.co.jp/listing/stocks/delisted/index.html)は上場廃止日、銘柄名、
  code、市場区分、理由を公開し、TOB、MBO、合併、株式交換・移転等の理由を区別できる。一覧だけでは
  cash consideration、交換比率、最終売買日、successor tickerを一意に再現できないため、exit valueは
  個別の会社・取引所一次開示への接続が必要である。pageは予定日も含み、一覧の対象外となる11年前分は
  統計月報へ誘導されるが、それより前のcoverageはこのpageから確認できない。したがって単独で全期間の
  point-in-time状態を表さない。

PDF本体のsample download、hash、format/correction inventory、代表eventの一次開示追跡は実施していない。
したがって対象期間のevent completenessとexit value resolved率は未評価である。

## 3. local SQLiteからの機械計算

SQLiteはURI `mode=ro`で読み、remote取得可能性の代替とはしない。

| source | rows | distinct dates | range | tickers |
| --- | ---: | ---: | --- | ---: |
| `jquants_master_snapshots` | 4,443 | 1 | 2026-05-13 | 4,443 |
| `jquants_daily_bars` | 5,219,443 | 1,211 | 2021-08-02〜2026-07-14 | 4,999 |
| `jquants_fin_summaries` | 90,149 | 1,218 | 2021-08-02〜2026-07-14 | 4,364 |

repository定数は`BARS_INPUT_WINDOW_DAYS=1200`、`FIN_INPUT_WINDOW_DAYS=730`である。
2019-10-31 cohortのrequired startは次のとおり機械計算した。

```text
bars: 2019-10-31 - 1200 days = 2016-07-18
financial summaries: 2019-10-31 - 730 days = 2017-10-31
```

`month_end_asof_grid`を既存daily barsへ適用すると、2021-08-31〜2026-06-30の59 cohortである。
2026-07-16時点で5y targetが満期済みのlocal cohortは0件で、design / confirmへ分割できない。
これは現在SQLiteだけではB1の技術的feasibilityすら示せないことを意味する。authorized remote historyに
より古いbars / master / financial summariesが存在する可能性は否定しない。

再計算command:

```bash
sqlite3 -header -column 'file:stores/market/market.sqlite?mode=ro' \
  "SELECT COUNT(*), COUNT(DISTINCT snapshot_date), MIN(snapshot_date), MAX(snapshot_date),
          COUNT(DISTINCT ticker) FROM jquants_master_snapshots;"

sqlite3 -header -column 'file:stores/market/market.sqlite?mode=ro' \
  "SELECT COUNT(*), COUNT(DISTINCT traded_at), MIN(traded_at), MAX(traded_at),
          COUNT(DISTINCT ticker) FROM jquants_daily_bars;"

sqlite3 -header -column 'file:stores/market/market.sqlite?mode=ro' \
  "SELECT COUNT(*), COUNT(DISTINCT disclosed_at), MIN(disclosed_at), MAX(disclosed_at),
          COUNT(DISTINCT ticker) FROM jquants_fin_summaries;"

UV_CACHE_DIR=/tmp/uv-cache uv run python -c \
  "from datetime import date, timedelta; from pathlib import Path;
from baibai_loop.screening.calibration.grid import month_end_asof_grid;
from baibai_loop.screening.calibration.horizons import require_horizon;
from baibai_loop.screening.metrics import BARS_INPUT_WINDOW_DAYS, FIN_INPUT_WINDOW_DAYS;
grid = month_end_asof_grid(Path('stores/market/market.sqlite'), start=date(2019,1,1),
                           end=date(2026,7,14));
matured = [d for d in grid if require_horizon('5y').target_date(d) <= date(2026,7,16)];
print(len(grid), grid[0], grid[-1], len(matured),
      date(2019,10,31)-timedelta(days=BARS_INPUT_WINDOW_DAYS),
      date(2019,10,31)-timedelta(days=FIN_INPUT_WINDOW_DAYS))"
```

## 4. 事前登録基準に対する結果

| 判定項目 | 結果 | 根拠 |
| --- | --- | --- |
| authorized historyから5y満期済みcohortを1件以上構成 | 未確認 | plan / API access未確認 |
| actual cohortとrequired rangeの機械計算 | localのみ完了 | remote lower boundと結合できない |
| design / confirm分割 | 不可 | local 5y matured n=0、remote n不明 |
| exact master Date / population / reject検証 | 実装済み・remote未実施 | #419、API request 0 |
| trading-day gap | 未確認 | authorized historical rowsなし |
| 月報のformat / correction inventory | 未実施 | PDF sample/hashなし |
| 代表eventのexit value | 未実施 | resolved / unresolved件数なし |
| 対象期間event completeness | 未定義 | static一覧だけでは不足 |
| license / retention /費用 | 未承認 | 公開FAQだけでaccount契約を代替しない |

`adopt for B1`は全条件を満たさない。`source non-adoption`は代表eventを調査していないため断定しない。
Issueの第三分岐`blocked`だけが根拠と一致する。

## 5. 後続判断

本監査でB1を起票しない。#417のADV 0.5億円variantは、master append-onlyだけでなくmembership、
delisting、corporate-action event、exit valueを含む3y / 5y evidenceがeligibleになるまで着手条件を
満たさない。単一as-of、3m / 6m / 1y、post-hoc ranking流入で代替しない。

再開には次の人間回答を必要とする。

1. 現在契約中のplan名またはtier。
2. historical endpointを最小1 requestだけ確認し、raw responseをlocal-onlyに一時保存する許可。
3. 保存path、retention、削除条件。提案defaultは`.cache/investigations/420/`、Git対象外、
   reportの事実照合完了後に削除、認証情報を保存しない。
4. Standardで必要rangeを確保できない場合にPremium費用判断へ進むか。

回答が得られた場合は、このblocked reportを結果で上書きせず、新しいIssueで改めてsource確認前の
採否基準と最大scopeを事前登録する。回答がない限り、raw capture、parser、event ledger、backfill、
production-purpose calibrationを実行しない。

## 6. 変更していないもの

- production code / provider / store / schema / validator
- production `stores/market/market.sqlite`
- calibration panel / forward cache / horizon authority
- screening rules / selection / E[r] / FV
- J-Quants plan / contract

このreportは確認不能なsourceを`complete`へ昇格させず、production changeを許可しない。
