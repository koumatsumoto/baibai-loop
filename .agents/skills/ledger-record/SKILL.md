---
name: ledger-record
description: 人間が確認した注文・約定・資金の事実をdraftで確認し、台帳へ記録する。
---

# Ledger Record

確認済み事実をapplication DBへ追記し、cash・予約・保有をreplayする。提案/Reviewから採用・注文・約定を推測せず、broker残高を自動取得/推定/完全照合しない。価格はmarketで読み台帳に複製しない。

DB更新は作業コピーへの保存で、クラウド反映とは別。[application手順](../../../jobs/OPERATIONS.md#application-db-を反映する)で開始/再開・保存後反映を行う。

## 事実からdraftを作る

| 報告 | 入口 |
| --- | --- |
| 買付注文・約定、売付の未約定注文 | `portfolio broker-fact-draft`（売付は`--side sell`） |
| 売却・部分売却の約定 | `portfolio sell-execution-draft` |
| 入出金・income・費用・確定税 | `portfolio event-draft` |
| 人間のwarning受容・税の見積り | `portfolio override-draft`・`portfolio meta-draft` |

初回残高/既存保有のbootstrap CLIは未提供で、通常入金/約定として再登録しない。業種等の既知入力は既存sourceで確認する。

### 事実と再送

eventはappend-onlyで、遅延報告でも既存ID/同時刻順を変えない。日付由来のoccurred_atは順序キーで実時刻でない。実時刻が分かる場合だけ`--occurred-at`を使い、時刻/期限を記帳の前提にしない。「今週中」/route等の原文から期限/実約定市場を推定しない。

約定は報告の日付・銘柄・数量・単価を使う。IDなしの同日同内容は再送とし、別約定との区別がつかない場合だけ確認する。broker execution ID（注文内のみ一意なら注文IDとの組）で識別し、注文ID単独/推定時刻を使わない。同ID同内容は予約消滅後もno-change、異内容は拒否する。

買付は具体buyの公開Proposal IDと確認済み実注文条件を使う。

```bash
uv run baibai-engine portfolio broker-fact-draft --db stores/application/baibai.sqlite \
  --decision-reference <INVESTMENT_PROPOSAL_ID> --status open \
  --ticker <TICKER> --quantity <QTY> --price-guard-yen <GUARD> \
  --occurred-on <YYYY-MM-DD> --sector <SECTOR> --out <DRAFT>
```

提案なしの実買付は`--direct-fact`へ替え、初回manual-fact参照を後続/再試行へ引継ぐ。記帳のために評価/提案を作らず、旧bindingを新規評価としない。

後続は同じ参照・reservation・orderへ結び、別のactive注文を推測しない。実売却の判断参照は任意。

```bash
uv run baibai-engine portfolio sell-execution-draft --db stores/application/baibai.sqlite \
  --ticker <TICKER> --quantity <QTY> --price-yen <PRICE> \
  --occurred-on <YYYY-MM-DD> --execution-id <BROKER_EXECUTION_ID> --out <DRAFT>
```

<a id="human-result-semantics"></a>
### 予約・約定・cash

買予約は未予約cash、売予約は保有数量を拘束し、partial fill後は残注文を保つ。見込代金/未約定数量でcash/lotを変えず、取消/失効は人間確認のreleaseで解放する。時刻だけでは解放しない。

注文未登録の直接約定も記帳でき、架空の発注時刻/注文を補わない。非拘束株の独立売却で他予約を減らさず、拘束株なら対応注文または取消事実を確認する。

売買/incomeはgross、費用/確認税は別event。売却cashは約定時に計上するがavailable cashは未予約現金で、受渡済み資金/broker発注可能額ではない。FIFO実現gross損益は開始から通算し、全売却/買直しでリセットしたりcashへ再加算したりしない。

予定入金/未報告取引は加算しない。明示費用0は記録できるが、eventなしは費用/税ゼロの確認でない。推定exit taxは表示だけでcash/confirmed taxと分ける。

## 確認と反映

draftのpayloadとcash・予約・保有の差分を人間に確認し、`portfolio apply-draft <DRAFT> --db stores/application/baibai.sqlite --confirmed`で適用する。staleは再作成/再確認し、矛盾時は書かない。記帳は報告の整合を検証し現在の購入適格性を再審査しない。Operation開始・無関係なmarket更新/充足は不要。

未作成/未登録と破損を分け、不正storeを空保有にしない。現在条件で過去取引を変えず、[復元](../../../jobs/OPERATIONS.md#application-db-を復元する)へ戻る。

## 時価と権利basis

raw closeと保有episodeの数量basisを合わせ、[価格source](../../../docs/reference/model-observations.md#public-research-reports)を守る。日次終値に架空時刻を付けず立会終了による利用可否と観測日を分ける。価格調整から株数/rights行使を推測しない。終値日後も評価日までの受領済みactionを確認する。当日bar不在でも既知分割は扱うが、休場/未到着から変更を作らず未解決数量を自動換算しない。

quote不足/古さ/権利未解決は該当時価を未評価とし、cash・予約・数量・原価を保つ。全保有を評価できなければNAV/集中比率を作らず、cash-only/旧台帳価格で埋めない。再評価でledger_as_ofを進めない。

保有中の受領済み分割/併合等は、記帳上の全売却や買直しでも解決したとみなさない。該当時は現在評価と新規提案を止め、DashboardのNAV/実現損益を未評価にする。これは通常の現金のみ・全売却や価格欠測を一律停止する制約ではない。確認済み数量変更の記帳契約は未対応で、原本係数から自動補正したり架空の売買/入出金で調整したりしない。

<a id="historical-outcome"></a>
## Historical Outcome

JPX営業日closeごとに全資産をreplayする。入出金だけexternal flow、売買/予約/income/費用/税は内部event。記録済み費用/税だけを反映し、resolvedでも未報告分の不存在を保証しない。

開始日closeから終了日closeを測り、開始NAVのeventを期間flowへ重ねない。非営業日eventは次営業日BODへ繰り越す。開始前分割/全売却日のactionも含むepisodeを確認するが、購入当日actionを過去保有の変更にしない。calendar・価格・basisは同じSQLite snapshotを使う。

開始前に全売却と記帳されても、保有中に受領済みの株数変更が未解決ならcash-onlyの実績にしない。この確認だけのために過去全期間の株価を要求せず、計測終了後の取引は持ち込まない。

価格/権利/分母不足や不正NAVは補完せずunresolved。確認済み損失/費用で正の資本が0になった場合は-100%の実績で、欠測でない。

primary benchmarkは同期間のJPX TOPIX gross total return。ETF price-onlyを代用せず、portfolioとの税/費用差を示す。売却済み銘柄/勝率だけで成果を測らない。

依頼時に確認したbenchmarkを[BenchmarkObservation](../../../engine/src/baibai_engine/machine_analysis/observations/prices/benchmark_observation.py) YAMLへ記す。期間/horizon・公表/受領時計・公式sourceを揃え、年率の要否/整合は同契約に従う。[fixture](../../../tests/fixtures/benchmark-observation/topix-1y.yaml)は形式例で実績へ流用しない。

```bash
uv run baibai-engine portfolio outcome --db stores/application/baibai.sqlite \
  --sqlite stores/market/market.sqlite --benchmark-observation <BENCHMARK_YAML> \
  --out <NEW_OUTCOME_YAML>
```

resolvedだけapplication DBへimmutable保存する。`--out`は任意exportで既存pathを上書きしない。export失敗はDB保存の取消ではない。過去IDを現在値/新形式で書換えず、遅延報告等で再計測するなら`--outcome-id <NEW_OUTCOME_ID>`、同内容再送なら同じIDを使う。
