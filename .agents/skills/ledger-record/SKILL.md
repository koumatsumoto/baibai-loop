---
name: ledger-record
description: 人間が確認した注文・約定・資金の事実をdraftで確認し、台帳へ記録する。
---

# Ledger Record

確認済み取引・資本をapplication DBのeventとして保存し、cash・予約・保有をreplayする。Reviewや提案から人間の採用・発注・約定・cashを推測せず、broker残高の自動取得・推定・完全照合もしない。価格は評価時にmarketから読み、台帳へ第二の価格正本を置かない。

クラウド正本へ反映する作業は、開始前に[application作業コピーの準備](../../../jobs/OPERATIONS.md#application-db-を反映する)を行い、DBと取得ETagを揃える。既存作業の再開ではその組を保持する。

## 事実からdraftを作る

| 報告 | 入口 |
| --- | --- |
| 買付注文・約定、売付の未約定注文 | `portfolio broker-fact-draft`（売付は`--side sell`） |
| 売却・部分売却の約定 | `portfolio sell-execution-draft` |
| 入出金・income・費用・確定税 | `portfolio event-draft` |
| 人間のwarning受容・税の見積り | `portfolio override-draft`・`portfolio meta-draft` |

初期化済み台帳への追記を対象とする。初回残高・既存保有を一括登録するbootstrap CLIは未提供であり、通常の入金・約定として再登録しない。業種等の既知入力は既存sourceから確認する。

### 事実と再送

eventはappend-only。遅延報告も新規rowとしてreplay順へ追加し、既存IDや同時刻順を変えない。日付報告の内部occurred_atは順序キーで、実発注・約定時刻の推定ではない。任意の時刻・期限を記録の前提にせず、正確な報告時刻は`--occurred-at`へ渡せる。「今週中」やrouteは原文で残せるが正確な期限・実約定市場へ変換しない。

約定は報告された日付・銘柄・数量・単価を使う。同じ日付・内容のIDなし報告は再送として扱い、同日別約定を区別できない場合だけ確認する。区別にはbrokerのexecution IDを使い、注文内だけで一意なら注文IDとの組を渡す。注文ID単独や推定時刻では区別しない。同ID同内容は予約消滅後もno-change、異内容は拒否する。

買付は具体buyを公開したTrade Proposal IDと人間が確認した実注文条件を使う。

```bash
uv run baibai-engine portfolio broker-fact-draft --db stores/application/baibai.sqlite \
  --decision-reference <INVESTMENT_PROPOSAL_ID> --status open \
  --ticker <TICKER> --quantity <QTY> --price-guard-yen <GUARD> \
  --occurred-on <YYYY-MM-DD> --sector <SECTOR> --out <DRAFT>
```

提案なしの実買付は`--decision-reference`の代わりに`--direct-fact`を使う。初回のmanual-fact参照を後続・再試行で保持し、同じ報告へ新しい参照を発行しない。事実記録のために評価・提案を作らず、旧bindingを新規評価として読まない。

後続の注文・約定は同じ参照・reservation・orderへ結び、別のactive注文へ推定対応付けしない。実売却の判断参照は任意である。

```bash
uv run baibai-engine portfolio sell-execution-draft --db stores/application/baibai.sqlite \
  --ticker <TICKER> --quantity <QTY> --price-yen <PRICE> \
  --occurred-on <YYYY-MM-DD> --execution-id <BROKER_EXECUTION_ID> --out <DRAFT>
```

<a id="human-result-semantics"></a>
### 予約・約定・cash

買予約は残数量×guardを未予約cashから拘束し、売予約は保有数量を拘束する。売付の見込代金をcashへ加えず、約定まではlotを減らさない。partial fill後は未約定分だけを残し、取消・失効は人間確認のreleaseで解放する。時刻だけから自動releaseしない。

保存注文へ対応する約定と注文未登録の直接約定を記録でき、発注時刻や架空の注文を補完しない。他注文に拘束されていない株数の独立売却でも既存予約を勝手に減らさず、拘束済み株数の報告は対応注文または取消事実を確認する。

売買代金とincomeはgross、費用と確認税は別event。売却代金は約定時にcashへ計上し、available cashは台帳上の未予約現金であって受渡済み資金・broker発注可能額ではない。FIFOの実現gross損益は台帳開始から通算し、全売却・買直しでリセットせず、既にcashへ入った売却損益を再加算しない。

予定入金や未報告取引はcashへ加えない。明示された費用0は0 eventとして保存できるが、eventなしは費用・税ゼロの確認ではない。推定exit taxは表示計算でcashやconfirmed taxと混ぜない。

## 確認と反映

draftは正本を変更せず、確認した入力状態と差分を持つ。payloadとcash・予約・holdingの差分を人間に確認し、`portfolio apply-draft <DRAFT> --db stores/application/baibai.sqlite --confirmed`で適用する。staleや矛盾では書かず、staleなら新しい入力からdraftを作り直して再確認する。現在の購入適格性で実取引を再審査せず、cash・数量・参照・報告の整合を検証する。取引記録のためにOperationを開始せず、無関係なmarket不足・更新で止めない。

台帳読取は未作成/未登録と破損を区別し、table欠損・不正payload・metadataなし取引を空保有へ変換しない。現在の判断・注文条件から過去取引を改変しない。復旧は[OPERATIONS](../../../jobs/OPERATIONS.md#application-db-を復元する)に従う。

本番へ戻す場合は[application反映](../../../jobs/OPERATIONS.md#application-db-を反映する)へ進む。作業コピーへの適用・保存とクラウド正本の更新を区別する。

## 時価と権利basis

raw/unadjusted closeと現在保有episodeの数量basisを合わせる。取得済み日次終値へ架空の15:30観測時刻を付けず、立会終了による利用可否と観測日を分ける。価格調整basisと株数basisは別で、rights issueから既存数量や権利行使を推定しない。終値日より後も評価日までの受領済み株数変更を確認し、当日取引がなくても既知の分割を無視しない。当日bar未到着や休場だけで株数変更を推測せず、未解決の数量は自動換算しない。

quote不足・古さ・権利未解決は対象holdingの時価を未評価にする。cash・予約・数量・原価を保持し、全保有が評価できなければNAV・集中比率を作らない。cash-only代用や旧台帳価格で穴埋めしない。再評価だけでledger_as_ofを進めない。

<a id="historical-outcome"></a>
## Historical Outcome

資産全体を各JPX営業日closeまでreplayし、cash・予約・open holding時価からNAVを求める。入金/出金はexternal flow、売買・予約・income・cost・taxは内部event。実費・税は記録済みだけを反映し、resolvedも未報告分の不存在を保証しない。

期間は開始日closeから終了日close。開始NAVへ反映済みeventを期間cashflowへ重ねず、非営業日eventは次の営業日BODへ繰り越す。開始前の分割と全売却日のactionも含む保有episodeの単位を確認するが、購入当日のactionを過去保有の変更としない。calendar・価格・basisは同じSQLite読取snapshotを使う。

価格欠損、未解決action、不正NAV・分母は補完せずunresolvedにする。確認済み損失・費用で正の資本が0になった場合は欠測でなく-100%の実績。

primary benchmarkは同期間のJPX TOPIX gross total returnで、ETF price-onlyの市場方向評価を転用しない。指数とportfolioの税・費用basis差を示し、売却済み銘柄や勝率だけで成果を測らない。

Outcomeが依頼されたら、このbenchmarkを確認した[BenchmarkObservation](../../../engine/src/baibai_engine/machine_analysis/observations/prices/benchmark_observation.py)形式のYAMLを用意する。[fixture](../../../tests/fixtures/benchmark-observation/topix-1y.yaml)は形式例であり、実績観測として流用しない。期間・horizon・公表日・受領時刻・公式sourceを揃え、1yの年率はnull、3y/5yは累積returnと整合する年率を記す。

```bash
uv run baibai-engine portfolio outcome --db stores/application/baibai.sqlite \
  --sqlite stores/market/market.sqlite --benchmark-observation <BENCHMARK_YAML> \
  --out <NEW_OUTCOME_YAML>
```

resolved時は作業application DBへimmutableなOutcomeを保存し、unresolved時はDBへ保存しない。`--out`は任意のexportで、既存pathを上書きしない。本番へ戻す場合は前述のapplication反映手順を使う。

過去Outcome IDへ新形式や現在値を上書きしない。遅延報告の費用・確定税等を反映して同期間を再計測するときは、`--outcome-id <NEW_OUTCOME_ID>`で以前の結果を残し、同内容の再送には同じIDを使う。
