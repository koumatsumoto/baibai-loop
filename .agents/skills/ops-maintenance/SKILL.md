---
name: ops-maintenance
description: 機械の健全性維持。daily batch 監視、store 同期（R2 push/pull）、materialize、障害対応、calibration panel と PMI manifest の月次維持、運用 task の規約。
---

# Ops Maintenance

投資判断はしない。事実の生成・検証・配信の再現性を守る。

## Daily batch 監視

`cloud-daily-batch` が東証営業日 16:43 JST に coverage → EDINET 抽出 → run → select → macro refresh → export → prune を 1 コマンドで回す（契約は [`batch/OPERATIONS.md`](../../../batch/OPERATIONS.md)）。Discord `#batch-runs` の `[OK]` / 失敗通知に当日の差分件数が載る。

push の経路は 2 本ある。run が起動すれば run 自身が結果を通知し、起動しなければ `cloud-batch-watchdog`（平日 21:00 JST）が同じ channel へ `[MISSING]` を送る。したがって **`#batch-runs` の沈黙は「当日の batch が正常だった」を意味する**。UI の as-of と workflow 履歴は裏取り用の pull 経路であって、欠測の第一発見手段ではない。

失敗時の入口:

- **source 取得失敗**: Baibai Loop Macro タブ上部の要約カード（取得失敗・stale 件数）→ 該当行。系列別の成否は `provider_runs`。取得失敗を「未公表」と混同しない。Tier 1 が取れないときは [`data-sources.md`](../../../docs/reference/data-sources.md) の Tier 2 例外運用。
- **validation 失敗**: application service / DB constraint / model validation の error path を読み、schema・validator の意味を推測で変えない（必要なら issue）。
- **automation 失敗**: screening CLI は [`screening-runtime.md`](../../../docs/reference/screening-runtime.md)、バッチ経路は [`architecture.md#cloud-serving-layer`](../../../docs/architecture.md#cloud-serving-layer)、CI/local parity は [`python-foundation.md`](../../../docs/reference/python-foundation.md)。

- **欠測（`[MISSING]` が届く / 何も届かない）**: schedule run が起動していない。`gh run list --workflow=cloud-daily-batch.yml` で当日の run を確認し、無ければ下記の手順で当日分を dispatch する。過去日の判定をやり直すときは `gh workflow run cloud-batch-watchdog.yml -f check_date=<YYYY-MM-DD>`（その日の 21:00 JST に発火した watchdog と同じ窓を評価する）。

失敗 run は publish が skip され正本は変わらない。復旧後の再実行は `gh workflow run cloud-daily-batch`（必要なら `MANUAL_ASOF` dispatch input）。**古い workflow revision の rerun は使わない**（main の現行コードで dispatch し直す）。復旧 dispatch が成功すれば watchdog の窓に入るので、当日中の復旧なら警報は出ない。

## 研究済み FV への価格到達（毎営業日）

深掘りを終えた lane は、buy / defer / reject を問わず研究 FV を持つ。価格がそこへ降りてきたことに気づく経路が無いと、一次情報まで降りて出した FV が誰も読まない値になる。batch 監視と同じ頻度で次を回す。

```bash
uv run python -m baibai_engine.research_watch \
  --db stores/application/baibai.sqlite --sqlite-path stores/market/market.sqlite --asof <最新完全営業日>
```

`triggered` に出るのは **未保有で終値が研究 FV 以下**の lane だけである。保有中の「FV 未満」は value 保有の定常状態で毎日出続けるため、ここには入れない（保有側の FV 到達は close ≥ FV で、`holding-review` の trigger である）。

triggered は注文ではなく「読み直す理由が発生した」の合図なので、`research` skill の再評価へ入り、thesis の前提が今も成立するかを確かめてから plan-limit を起こす。出力は全行 `re_research_required: true` を返す（thesis の鮮度に関わらず再研究を挟む規律であって、行を選り分ける flag ではない）。価格だけを見て発注しない。

## 指値規律の成績（注文が決着したとき）

注文が約定または失効したら、その 1 件だけでなく全体を並べ直す。

```bash
uv run python -m tools.experiments.measure_limit_outcomes --asof <最新完全営業日>
```

`summary.decision_bound_orders` が repository の判断経路を通った注文の成績で、`all_ledger_orders` は既存保有の取り込みを含む。**取り込み分の約定を規律の成績に数えない。** `forgone_pct` は失効後 20 立会日の窓が満ちた注文にだけ付き、途中の注文は `forgone_pending_window_orders` に数えられる（部分観測を「逃した幅」として読まない）。 `chase_policy_decision.ready` が true になったら、gap を追う指値へ変えるかを別 issue で事前登録して判断する。false のうちは個票を並べるだけにして、少数の失効で規律を外さない。

## Store 同期（`batch/scripts/r2_transfer.sh`）

| store | 正本 | 転送規律 |
| --- | --- | --- |
| market / machine（runs） | R2 | 読む前に `pull-market` / `pull-machine` → **`hydrate-market`**。R2 の market copy は lake が持たない 4 table だけを運ぶので、pull しただけの store には価格も財務も 1 行も無い。push は lake 由来 15 table を `publish-lake` で先に出し、その後 `push-market` が残り 4 table を R2 copy と merge してから upload する（merge-then-push）。ローカルだけで長く作業した store を直接 push しない |
| macro（indicators） | R2 | 同上（`push-macro` は no-loss merge。誤値の訂正は削除でなく `macro retract` — 契約は [`macro.md`](../../../docs/reference/macro.md)） |
| app（baibai.sqlite） | **local** | 判断はローカルが正本。publish 後に `push-app`（直 push）→ materialize。**pull しない** — `pull-app` はローカルに store があれば止まる（cloud copy で置換すると未 push の判断が消える）|

**pull は batch の走行中を避ける。** 3 store は順に download されるので、その途中で batch が push すると batch 前後の世代が混ざった断面がローカルへ載る。`changed on R2 during the pull` で止まったらそれで、batch の完了を待って引き直す（ローカルの store は置換されていない）。避けるべき窓の導出は [`batch/OPERATIONS.md`](../../../batch/OPERATIONS.md)。

cloud 障害は「store が code より古い」形で出ることが多い。再現はローカルへ R2 store を pull して read 経路を通す。

## Materialize（serving 反映）

app / macro を publish したら `gh workflow run cloud-materialize` を dispatch し、run の completed success を確認する。view shape を変える deploy では **code deploy → materialize の順**を守り、UI は旧 view で graceful degrade できることを確認する。

**Actions が runner を取れないときはローカルで同じ 3 手順を回す。** workflow は pull → export → upload の 3 段でしかないので、正本がローカルにある状態なら pull を省いて残り 2 段を実行すれば結果は同じになる。

```bash
uv run python -m baibai_web.materialize --output-dir <dir> --batch manual
batch/scripts/r2_transfer.sh upload-serving <dir>
```

`<dir>` は使い捨ての作業ディレクトリにする。upload 後は read 経路を 1 つ踏んで確認する。

**machine store が古いまま export しない。** export は app と machine の両方を読み、serving の `screening_latest.run` / `rows` は machine 側から来る。ローカルで `screening run` を打った直後にここを回すと、cloud の runs store に無い run が serving へ出る。`runs.sqlite` は cloud が唯一の writer なので push でも揃えられない（`push-machine` は Actions 専用）。判断（shortlist / session / task）だけを反映したいならそれで足りる — app 由来の view は正しく更新される。machine 側も揃えたいときは `pull-machine` を先に回すが、**ローカルの run は消える**ので、まだ参照する selection があるなら先に `selection show` で控える。

## 取得枠 store の初期充填

`refresh-buyback-reports` は日次 batch が毎日回すが、初回だけは 6,500 件超の遡及があり、EDINET が
`429` を返して途中で止まる。**止まっても成果は残る**（保存済みの提出は二度と取りに行かない）ので、
`rate_limited=false` になるまで繰り返す。cloud へ載せるところまでが 1 セットである。

```bash
batch/scripts/r2_transfer.sh pull-market
batch/scripts/r2_transfer.sh hydrate-market
uv run baibai-engine screening refresh-buyback-reports --asof <最新完全営業日> --lookback-days 400
batch/scripts/r2_transfer.sh publish-lake
batch/scripts/r2_transfer.sh push-market
```

`edinet_buyback_reports` は lake 所有なので、hydrate せずに走らせると空の store へ書き、
`publish-lake` を飛ばすと `push-market` が「release が持つ行数と合わない」で止まる。

**完了の判定は行数が動かなくなることで、`considered` が 0 になることではない。** store は 1 銘柄 1
報告月につき勝った提出しか覚えないので、同じ月を別の提出（訂正）が上書きすると、上書きされた側は
次の pass で「未保存」に見えて読み直される。したがって `considered` と `stored` は毎回同じ値で
下げ止まる（実測 1,032 銘柄 6,067 行で `considered=515` / `stored=132`）。同じ pass を 2 回続けて
`(銘柄, 報告月) → 提出` の対応が 1 件も変わらなければ不動点である。

`unreadable` は様式が読めなかった件数で 0 にはならない（取締役会決議の節を持たない提出が含まれる）。
`without_usable_month` は報告月が読めなかったか、読めた月が提出日より後だった件数で、後者は日付が
別の行に噛んだ証拠なのでその読みを丸ごと捨てている。

## 月次維持

- **calibration panel**: `uv run baibai-engine screening calibration-build --start 2022-09-01 --end <直近の完全月末>`（増分。rules 改訂後は `--force` 再構築）→ `calibration-evaluate`。契約は [`estimate-calibration.md`](../../../docs/reference/estimate-calibration.md)。
- **PMI manifest**: `uv run python -m baibai_engine.macro.indicators.pmi_manifest --dry-run` → 本実行 → `macro refresh` で該当月を取得し公表値と照合してから commit。月が飛ぶ追記は拒否される（先に穴を埋める）。
- **lake の全 history 照合**: 週次で lake export の `--audit` を回す（正確な command は [`market-lake.md`](../../../docs/reference/market-lake.md#local-pipeline-の実測)）。日次の publish は「その build が書いた月」しか SQLite ↔ Parquet parity を見ないので、日次 window の外側で store を訂正した月は次の `--audit` まで lake に映らない。これが release と SQLite が全期間で一致することを問う唯一の操作である。実測は全量 436 秒。
- **資本配分・支配権イベント**: 東証の開示企業一覧は毎月 15 日前後に更新される。`uv run baibai-engine screening refresh-capital-control --asof <ASOF>` → `uv run baibai-engine screening build-control-event-exits --asof <ASOF>`。前者は東証一覧と JPX 上場廃止を読み直し、後者は成立した公開買付けの実現 exit 値を導出する。どちらも繰り返し実行して同じ結果になる。出力の `tse_sheets` は取り込めた月次シート数で、前月から増えていなければ東証側がまだ更新していない（減っていたら最古シートが落ちたということなので、既存の月は store に残る）。`build-control-event-exits` の rejection 内訳は「一次資料が案件を一意に決められなかった件数」であり、0 になる性質のものではない。実行後は `push-market` で R2 正本へ同期する（この 2 つが書く 3 table は lake 所有ではないので `publish-lake` は要らないが、`push-market` は hydrate 済みの store を要求する — 先に `hydrate-market` を通す）。契約は [`screening-runtime.md`](../../../docs/reference/screening-runtime.md#資本配分支配権イベントの-typed-fact)。

## 運用 task の規約

運用 task の正本は application DB（`baibai-engine task` が唯一の writer）。GitHub Issue は開発作業（feature / bug / 基盤改善）に使い、task 管理には使わない。

- **対象**: defer thesis の再確認 event、決算後の holding review、実行日付きの判断待ち、注文期限後の運用確認。日付のない改善案は対象外（issue へ）。
- **粒度**: 同じ期限・種別・領域の未保有候補は 1 件に統合。保有・売買判断・高重要は個別。追加前に `task list --status open` で既存と照合する。
- **body**: load-bearing question / primary sources / expected destination / close condition だけを短く。作業ログを複製しない。
- **状態**: `done`（判断と canonical 更新完了）/ `dropped`（問いが不成立）。判断が変わった理由は thesis 等の正本へ書く。
- **resume**: `uv run baibai-engine task list --status open` を due 順に読み、current canonical entity・ledger と照合して trigger 到来の task を 1 件選ぶ。矛盾したら停止し、推定で終端へ進めない。
