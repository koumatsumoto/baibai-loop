---
name: ledger-record
description: 人間が確認した注文・約定・資金の事実をdraftで確認し、台帳へ記録する。
---

# Ledger Record

確認済み事実をapplication DBへ追記し、cash・予約・保有をreplayする。提案/Reviewから採用・注文・約定を推測せず、broker残高の自動取得/推定/完全照合もしない。価格はmarketで読み、台帳へ複製しない。

開始・再開・保存後のクラウド反映は[application作業手順](../../../jobs/OPERATIONS.md#application-db-を反映する)に従う。以下のDB更新は作業コピーへの保存で、クラウド正本への反映とは別である。

## 事実からdraftを作る

| 報告 | 入口 |
| --- | --- |
| 買付注文・約定、売付の未約定注文 | `portfolio broker-fact-draft`（売付は`--side sell`） |
| 売却・部分売却の約定 | `portfolio sell-execution-draft` |
| 入出金・income・費用・確定税 | `portfolio event-draft` |
| 人間のwarning受容・税の見積り | `portfolio override-draft`・`portfolio meta-draft` |

初期化済み台帳への追記用で、初回残高/既存保有のbootstrap CLIは未提供。通常入金/約定として再登録しない。業種等の既知入力は既存sourceで確認する。

### 事実と再送

eventはappend-only。遅延報告も追記し、既存ID/同時刻順を変えない。日付からのoccurred_atはreplay順序キーで実時刻でない。正確な時刻が分かる場合だけ`--occurred-at`を使い、時刻/期限を記帳の前提にしない。「今週中」/route等は原文を保ち、期限/実約定市場を推定しない。

約定は報告の日付・銘柄・数量・単価を使う。IDなしの同日同内容は再送で、別約定と区別できない場合だけ確認する。broker execution ID、注文内でのみ一意なら注文IDとの組で識別し、注文ID単独/推定時刻を使わない。同ID同内容は予約消滅後もno-change、異内容は拒否する。

買付は具体buyの公開Proposal IDと確認済み実注文条件を使う。

```bash
uv run baibai-engine portfolio broker-fact-draft --db stores/application/baibai.sqlite \
  --decision-reference <INVESTMENT_PROPOSAL_ID> --status open \
  --ticker <TICKER> --quantity <QTY> --price-guard-yen <GUARD> \
  --occurred-on <YYYY-MM-DD> --sector <SECTOR> --out <DRAFT>
```

提案なしの実買付は`--decision-reference`の代わりに`--direct-fact`を使い、初回manual-fact参照を後続/再試行へ引き継ぐ。記帳のために評価/提案を作らず、旧bindingを新規評価としない。

後続は同じ参照・reservation・orderへ結び、別のactive注文を推測しない。実売却の判断参照は任意。

```bash
uv run baibai-engine portfolio sell-execution-draft --db stores/application/baibai.sqlite \
  --ticker <TICKER> --quantity <QTY> --price-yen <PRICE> \
  --occurred-on <YYYY-MM-DD> --execution-id <BROKER_EXECUTION_ID> --out <DRAFT>
```

<a id="human-result-semantics"></a>
### 予約・約定・cash

買予約は残数量×guardを未予約cashから、売予約は保有数量を拘束する。売付の見込代金/未約定数量でcash/lotを変えず、partial fill後は残注文だけを保つ。取消/失効は人間確認のreleaseで解放し、時刻だけでは解放しない。

注文未登録の直接約定も記帳でき、架空の発注時刻/注文を補わない。非拘束株の独立売却で他予約を減らさず、拘束株なら対応注文または取消事実を確認する。

売買/incomeはgross、費用/確認税は別event。売却cashは約定時に計上するが、available cashは未予約現金で受渡済み資金/broker発注可能額ではない。FIFO実現gross損益は台帳開始から通算し、全売却/買直しでリセットせず、cashへ再加算しない。

予定入金/未報告取引は加算しない。明示費用0は記録できるが、eventなしは費用/税ゼロの確認でない。推定exit taxは表示だけでcash/confirmed taxと分ける。

## 確認と反映

draftは正本を変えず入力状態と差分を持つ。payloadとcash・予約・保有の差分を人間に確認し、`portfolio apply-draft <DRAFT> --db stores/application/baibai.sqlite --confirmed`で適用する。staleなら再作成/再確認し、矛盾時は書かない。記帳はcash・数量・参照・報告の整合を検証し、現在の購入適格性を再審査しない。Operation開始や無関係なmarket更新を要求せず、無関係なmarket不足で記帳を止めない。

未作成/未登録と破損を分け、table欠損・不正payload・metadataなし取引を空保有にしない。現在条件で過去取引を変えず、[復元手順](../../../jobs/OPERATIONS.md#application-db-を復元する)へ戻る。

## 時価と権利basis

raw closeと保有episodeの数量basisを合わせる。日次終値へ架空の15:30観測時刻を付けず、立会終了による利用可否と観測日を分ける。価格調整から株数/rights行使を推測しない。終値日後も評価日までの受領済みactionを確認し、当日barがなくても既知分割を扱うが、休場/未到着だけで変更を作らず未解決数量を自動換算しない。

quote不足/古さ/権利未解決は該当時価を未評価とし、cash・予約・数量・原価を保つ。全保有を評価できなければNAV/集中比率を作らず、cash-only/旧台帳価格で埋めない。再評価でledger_as_ofを進めない。

<a id="historical-outcome"></a>
## Historical Outcome

JPX営業日closeごとに資産全体をreplayしNAVを求める。入出金だけexternal flow、売買/予約/income/費用/税は内部event。記録済み費用/税だけを反映し、resolvedでも未報告分の不存在は保証しない。

開始日closeから終了日closeを測り、開始NAVのeventを期間flowへ重ねない。非営業日eventは次営業日BODへ繰り越す。開始前分割/全売却日のactionも含むepisodeを確認するが、購入当日actionを過去保有の変更にしない。calendar・価格・basisは同じSQLite snapshotを使う。

価格/権利/分母不足や不正NAVは補完せずunresolved。確認済み損失/費用で正の資本が0になった場合は-100%の実績で、欠測でない。

primary benchmarkは同期間のJPX TOPIX gross total return。ETF price-onlyを代用せず、portfolioとの税/費用差を示す。売却済み銘柄/勝率だけで成果を測らない。

依頼時に確認したbenchmarkを[BenchmarkObservation](../../../engine/src/baibai_engine/machine_analysis/observations/prices/benchmark_observation.py) YAMLへ記す。[fixture](../../../tests/fixtures/benchmark-observation/topix-1y.yaml)は形式例で実績へ流用しない。期間/horizon・公表/受領時計・公式sourceを揃え、1y年率はnull、3y/5y年率は累積returnと整合させる。

```bash
uv run baibai-engine portfolio outcome --db stores/application/baibai.sqlite \
  --sqlite stores/market/market.sqlite --benchmark-observation <BENCHMARK_YAML> \
  --out <NEW_OUTCOME_YAML>
```

resolvedだけapplication DBへimmutable保存する。`--out`は任意exportで既存pathを上書きしない。export失敗はDB保存の取消ではない。過去IDを現在値/新形式で書換えず、遅延報告等で再計測するなら`--outcome-id <NEW_OUTCOME_ID>`、同内容再送なら同じIDを使う。
