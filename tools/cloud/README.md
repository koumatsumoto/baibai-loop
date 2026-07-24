# tools/cloud — クラウド配信向けのローカルバッチ script

Cloudflare 配信（issue #467 の設計）の compute と転送の入口。ここにある Python / shell
script は GitHub Actions とローカル運用から呼ぶ orchestration で、stable CLI ではない
（安定契約は `baibai-engine` / `baibai-app` 側にある）。read model の生成と日次 batch は
ローカル単体でも実行でき、転送 script だけが R2 を使う。

## Cloudflare / GitHub Actions 構成

R2 bucketとobject keyは次の固定契約を使う。どちらのbucketもPublic Development URLとcustom domainを無効にする。

| bucket | object | owner |
| --- | --- | --- |
| `baibai-stores` | `market.sqlite` / `runs.sqlite` / `macro.sqlite` | `cloud-daily-batch` |
| `baibai-stores` | `baibai.sqlite` | ローカル`publish.sh`（replica） |
| `baibai-serving` | `views/*.json` | GitHub Actions materialize |
| `baibai-serving` | `history/select/<asof>.json` | 日次batch、削除しない |
| `baibai-serving` | `history/candidate-views/<asof>.json` | 日次batch、R2 lifecycleで31日後に削除 |

R2 lifecycle rule（31日削除）は`history/candidate-views/`へ追加する。旧形式の`history/candidates/` ruleは既存objectが31日で自然失効するまで残し、その後に削除する。Workerは旧prefixへ到達しない。

資格情報はprincipalごとに分ける。

| principal | 設定 | scope |
| --- | --- | --- |
| GitHub Actions | variable `R2_ACCOUNT_ID`、secrets `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / provider 3本、公開 JPX 規制 URL 4本 | stores + serving read-write |
| GitHub Actions（通知） | secret `DISCORD_WEBHOOK_URL` | `cloud-daily-batch` の通知 step のみ（job env に出さない） |
| ローカル`.env` | `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | storesだけread-write |
| Wrangler OAuth | `wrangler login` | bucket初期設定、Worker deploy、Worker secret |
| Worker secret | `VIEW_PASSWORD` | Worker runtimeだけ |

R2 S3 endpointは`https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`からscriptが組み立てる。credential、password、endpointの実値をGit、issue、logへ書かない。

provider secretは`JQUANTS_API_KEY` / `ESTAT_APP_ID` / `EDINET_API_KEY`。加えて日次batchが当日cache不足で`bootstrap-cache`へ入ると、JPX規制provider（`universe.required_jpx_flags`の4 source: 特別注意銘柄 / 整理銘柄 / 取引停止 / 上場廃止警告）が公開JPXページのURLを要求する。これらは非secretのため`cloud-daily-batch.yml`のjob envにliteralで置く（`JPX_SPECIAL_CAUTION_INDEX_URL` / `JPX_REORGANIZATION_URL` / `JPX_TRADING_HALT_URL` / `JPX_DELISTING_WARNING_URL`。雛形は`.env.sample`）。未配線だとbootstrapのJPX stepがfail-fastし、machine stores / serving uploadはskippedになる。

## 初回seedとWorker deploy

初回だけ、ローカル4 storeのconsistent SQLite snapshotをstores bucketへ送る。4 keyの
いずれかが既に存在する場合は、古いローカルcopyによる正本の巻き戻しを防ぐため何も
uploadせず停止する。

```bash
tools/cloud/seed.sh
```

WorkerはUIをbuildしてからdeployする。`VIEW_PASSWORD`はpassword manager等で生成した32文字のCSPRNG英数値を使い、値を引数やshell historyへ書かずpromptへ入力する。

```bash
cd cloud/worker
npm ci
npm run types:check
npm run typecheck
npm test
# 初回 deploy は secret 未設定時に全 API を 401 にする。
npm run deploy
npx wrangler secret put VIEW_PASSWORD
```

workflowがdefault branchに存在する状態で初回materializeを実行する。

```bash
gh workflow run cloud-materialize.yml --ref main
gh run list --workflow cloud-materialize.yml --limit 3
```

materialize完了後、passwordを画面表示・shell引数化せず、全API routeを未認証・誤認証・正認証で検査する。`VERIFY_TICKER`はservingに存在する4文字tickerへ必要に応じて変更する。

```bash
tools/cloud/verify_worker.sh
```

## 日常運用

application DBのjudgment更新をクラウド表示へ反映する。

```bash
tools/cloud/publish.sh
```

このscriptは`baibai.sqlite`のconsistent snapshotだけをstoresへ送り、`cloud-materialize`をdispatchする。servingへの直接writeは行わない。

decision-cycleやmacro分析を始める前に、クラウド正本のmachine storeをローカルへ取得する。

```bash
tools/cloud/pull.sh
uv run baibai-engine screening verify-cache-coverage --asof YYYY-MM-DD
uv run baibai-engine screening ticker-profile --ticker TICKER
```

`pull.sh`はmarket/runs/macroの全downloadとSQLite `quick_check`が成功してから3 storeを置換し、`baibai.sqlite`には触れない。日次batchと同時に実行して世代を跨がないよう、通常は18:30 JST前後とGitHub Actions実行中を避ける。

日次workflowを手動実行する。`asof`省略時は当日JSTをmarket calendarで判定し、非営業日は成功扱いでskipする。過去日を指定すると営業日gateをskipする。

```bash
gh workflow run cloud-daily-batch.yml --ref main
gh workflow run cloud-daily-batch.yml --ref main -f asof=YYYY-MM-DD
gh run list --workflow cloud-daily-batch.yml --limit 10
```

通常cronは平日09:30 UTC（18:30 JST）。株価日足の16:30 JST更新と、18:00 JST更新のJPX系日次datasetの後に余裕を置く。GitHub Actionsのschedule遅延は許容し、UIのas-ofとworkflow履歴で検知する。

`daily_batch.py`のexit 3はfresh screening exportを持つため、workflowはstores/serving uploadまで完了させてからjobを失敗にする。exit 1は新しいpublish可能runがないためuploadしない。非営業日skipはexportがないため既存servingを変更しない。

`*.workers.dev`のHTTP requestはWorkerが認証判定より前に308でHTTPSへredirectし、HTTPS responseはHSTSを返す。UI navigationは必ずこの経路を通り、hashed static assetだけをWorker invocationなしで配信する。

## Password rotation

```bash
cd cloud/worker
npx wrangler secret put VIEW_PASSWORD
```

新しい32文字CSPRNG値をpromptへ入力する。次のAPI 401でbrowserの旧値がlocalStorageから削除され、password入力画面へ戻る。Workerの再deploy、R2変更、application data更新は不要。

## R2 transferの安全境界

- upload前にPython `sqlite3.backup`でsnapshotを作り、WAL未checkpoint行を含めて`quick_check`する。
- 複数storeのpushは全snapshotの作成・検査を終えてからuploadを始める。machine storeのpushはGitHub Actionsからだけ許可する。
- 初回seedは既存のstore keyを1件でも検出したら停止し、再seedによるクラウド正本の上書きを許可しない。
- pullは固定4 key以外を受け付けず、全downloadと`quick_check`完了後に置換する。
- servingの`views/`は`aws s3 sync --delete`で完全像に合わせる。historyは追記だけで削除しない。
- `views/meta.json`は他のviewとhistoryが全て成功した後に最後にuploadする。
- bucket名は`R2_STORES_BUCKET` / `R2_SERVING_BUCKET`で明示的にoverrideできるが、通常は固定defaultを使う。

## export_read_models.py — read model の材料化

`baibai_app.readmodel` builders を共用して、UI が読む全 view を serving 配置どおりの
JSON に書き出す。Worker には業務ロジックを置かない設計の実体。

```bash
uv run python tools/cloud/export_read_models.py --output-dir <dir> [--batch daily|manual] [--repo-root <path>]
```

出力（`<dir>` 配下）:

- `views/dashboard.json` / `views/screening_latest.json` / `views/operations.json`
- `views/macro--<period>-<granularity>.json`（1y|5y|10y|max × daily|weekly|monthly|yearly）
- `views/security--<ticker>.json`（保有 + 最新 run 掲載 + shortlist の ticker）
- `views/meta.json`（生成時刻・実データ更新時刻・store 別 as-of・batch 種別。UI の鮮度表示と同じ契約）
- `history/select/<asof>.json`（machine selection のサマリ。無期限保持する軽量履歴）
- `history/candidate-views/<asof>.json`（run とCandidates全件を型付きUI read modelへ変換した履歴。31 日で削除）

`views/` は毎回 export の完全な像に置換される（実行のたびに一度削除して作り直すので、対象から外れた古い view は残らない）。`history/` は追記のみで、この script は削除を行わない。上記の「無期限保持 / 31 日で削除」は serving store（R2 lifecycle）側の保持契約であり、script の挙動ではない。

Workerは認証後の`/api/screening/history`でCandidates履歴の日付一覧を返し、`/api/screening/history/YYYY-MM-DD`だけを`history/candidate-views/`へ写像する。任意key、旧形式の`history/candidates/`、`history/select/`、store bucketは公開しない。

views の JSON は `baibai-app` の対応 API response と同形（pydantic `model_dump_json`）。`meta.json` は全 view / history の書き込み成功後に最後に書くので、途中失敗した出力 dir が新鮮さを主張する事態を避ける。

## daily_batch.py — 日次機械工程の 1 コマンド実行

営業日判定 → screening cache coverage（不足時のみ bootstrap）→ run → select →
macro series refresh + import-manual → export → run store prune を順に実行する。
全 step は public CLI の subprocess で、step ごとにコマンドライン・exit code・所要秒を
stdout へ出す（scheduled workflow のログをそのまま読む前提）。

```bash
# 通常（当日 JST。market calendar で非営業日なら exit 0 で skip）
uv run python tools/cloud/daily_batch.py --output-dir <dir>

# 手動再実行・過去日（営業日 gate を skip）
uv run python tools/cloud/daily_batch.py --asof YYYY-MM-DD --output-dir <dir>

# structured summary を書き出す（workflow の Discord 通知が読む）
uv run python tools/cloud/daily_batch.py --output-dir <dir> --summary-output <summary.json>
```

`--summary-output` を指定すると、success / skip / deferred / fatal の全終端パスで
`BatchExecutionSummary` JSON を atomic write する。screening / macro / serving-export / prune の
論理結果ごとに datasets・所要時間・metrics・typed errors を確定し、不正な `--asof` も
`invalid_asof` error を持つ fatal summary になる。stdout/stderr の既存診断はそのまま維持し、
summary には redaction 済みの typed errors だけを渡す。

終了コード:

| exit | 意味 |
| --- | --- |
| 0 | 完走。または非営業日（当日 gate で `skip` を出して即終了） |
| 1 | 致命的失敗で停止（screening chain・営業日判定・calendar 不備。publish に至らない） |
| 3 | export まで publish 済みだが、繰延べステップ（macro refresh / import-manual / prune）が失敗 |

失敗ポリシー:

- screening 系（coverage / run / select）の失敗は致命的で即停止する（publish できる新しい
  run が無いため exit 1）。ただし `screening run` の exit 2 は品質警告つきの published run で
  あり、警告理由を表示して続行する。`verify-cache-coverage` の exit 1 は cache 不足マーカーが
  ある場合だけ bootstrap へ進み、マーカー無しの exit 1（rules 破損等の crash）は即停止する
- macro series refresh の失敗は繰延べる: export まで完走して screening 結果は publish し、
  最後に exit 3 で終了する（scheduled workflow の失敗通知は発火し、鮮度は meta の
  `macro_asof` に現れる）。繰延べた失敗の詳細は発生時点で stderr にも出す
- `select` の前回 run 比較は、runs store の「target より前の最大 as-of の最新 revision」を
  この script が決定論的に解決して `--previous-run-revision-id` で渡す（同一日の再実行が
  複数 revision を作っても停止しない）
- 営業日判定は market store の `jquants_market_calendar` が情報源。対象日をカバーして
  いない場合は黙って続行せず明示エラーで停止する

## notify_discord.py — 日次 batch 結果の Discord 通知

`cloud-daily-batch` は run ごとに終端結果を Discord チャンネル `#batch-runs` へ1件通知する。
共通 Logger ではなく workflow 単位の通知 adapter で、チャンネルは code が選ばず repository
secret `DISCORD_WEBHOOK_URL` が指す webhook で固定する。workflow 末尾の単一 step
（`if: !cancelled()`）が、success / failure のどちらでも cancel 以外で1回だけ行う。

通知する結果は4種。

| label | overall outcome | 意味 | publish state |
| --- | --- | --- | --- |
| `[OK]` | succeeded | batch exit 0、local export あり、両 upload 成功 | published |
| `[SKIPPED]` | skipped_non_business_day | 非営業日 gate で skip（export なし） | not_generated |
| `[DEGRADED]` | published_with_deferred_failure | batch exit 3。screening は publish 済み、繰延べ step（macro / prune）が失敗 | published |
| `[FAILED]` | failed | 致命的失敗、batch 以外 step の失敗、summary 欠落・invalid・矛盾、upload 失敗 | upload step の status に従う |

判定の優先順は「batch 以外の step 失敗 → upload 失敗（`upload_failed`）→ batch summary の
outcome」。upload 失敗は batch が成功していても `[FAILED]` を優先する。setup/sync/pull/smoke の失敗は
batch 未到達（`not_started`）の `[FAILED]`、batch 実行後の summary 欠落・invalid は
`unavailable` の `[FAILED]`。summary が succeeded / degraded を主張しても observable な publish 状態
（local export / 両 upload 成功）が一致しない「矛盾」は、`summary_conflict` error を付けて
`[FAILED]` になる（静かな publish 劣化を `[OK]` として隠蔽しない）。exit 3 は publish 済みの
`[DEGRADED]`、upload 失敗は `[FAILED]`（`upload_failed`）という契約を README と test で固定する。

message には workflow 名・repository・trigger・run attempt・overall outcome・as-of・総所要時間・
batch ごとの status / datasets / metrics・publish state・GitHub Actions run URL を含む。error は
failed を degraded より先に表示し、4件以上は上位3件 + 残件数へ折りたたむ。error message は固定
code / stage / impact と検証済み scalar だけから作り、subprocess の stderr・例外本文・provider
response body は載せない（1行400文字以内、全体2000文字以内）。

`daily_batch.py` が `--summary-output` に書いた `BatchExecutionSummary` を読み、GitHub metadata と
step outcome を合成して `WorkflowRunSummary` を確定し、配送結果（delivered / failed）も記録して
atomic write してから、同じ model を Discord へ render する。notifier は repository dependency と
Python 3.14 固有構文を使わず、checkout 直後の system `python3` で import / CLI 実行できる
（setup-python 前の smoke step が `py_compile` と実 import の両方で検査する）。

通知の配送に失敗した run は、data 処理が成功していても job を失敗にする。`#batch-runs` に届かない
正常 run は配送失敗を意味するので、GitHub Actions の run log（通知 step の stderr）で data 処理の
成功と配送の失敗を区別して確認する。webhook URL・response body は log に出ない。未設定・HTTPS 以外・
Discord 以外の host・timeout・HTTP error はすべて sanitized な理由で non-zero 終了する。

### webhook rotation

`DISCORD_WEBHOOK_URL` は GitHub Actions の repository secret で、通知 step だけが読む（job env ・
CLI 引数・log には出ない）。secret の実値を Git・issue・log へ書かない。

1. Discord で `#batch-runs` の webhook を作り直す（または既存 webhook の token を再生成する）。
2. `gh secret set DISCORD_WEBHOOK_URL --repo <owner>/<repo>` で新しい URL を登録する。
3. 過去営業日の `asof` で手動 run を発火し、`#batch-runs` に1件届くことを確認する。

旧 webhook は Discord 側で削除するまで有効。

### 実配送の確認

`#batch-runs` への実配送は次で確認する。

- 不正な `asof`（例: `2026-13-99`）の手動 run → `[FAILED]`（`invalid_asof`）が1件届く。
- 有効な `asof` または次の通常 run → `[OK]` が1件届く。
- 非営業日が先に来た場合 → reason 付き `[SKIPPED]`（no-publish）が届く。

各 message の batch 名 / datasets / 件数 / 所要時間 / publish 状態 / run URL が正しいことを照合する。
