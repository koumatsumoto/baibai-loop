---
title: "Continuous decision cycle runbook"
summary: "前営業日終値の候補・指値提案、人間報告後のledger、保有review、年次評価をtrigger別に進める唯一のe2e入口。"
doc_type: operation
status: active
last_reviewed: 2026-07-12
related_docs:
  - "../doctrine.md"
  - "../portfolio-management.md"
  - "../workflow/README.md"
  - "../reference/decision-packet.md"
---

# Continuous decision cycle

Baibai-Loopの日常運用は「最もお買い得な日本株を見つけ、人間が発注判断できる状態にする」ことを目的とする。AIは観測、候補抽出、一次情報確認、5年評価、独立反証、最大許容価格、指値・数量、保有見直しを担当する。人間だけが`approve / defer / reject`とbroker操作を行う。

`opportunity`では一次リサーチの前に人間レビューgateを置く。audit poolから8〜10候補のshortlistレポートを提示し、人間が深掘り対象を選んでから、選択銘柄だけを個別リサーチする（OP3→OP4）。1銘柄へ先に決め打ちせず、比較可能な候補群を先に人間へ渡す。

価格判断にはJPX基盤の最新完全営業日のraw/unadjusted closeを使う。寄り前のrealtime quoteと板は必須入力ではない。AIは約定可能性や当日価格方向を予測しない。人間からbroker結果が報告されるまで注文状態を推定せず、ledgerを更新しない。

候補比較順は、(1)永久的資本毀損リスク、(2)5年期待総合returnとFV乖離、(3)repository portfolioへの追加価値、(4)購入可能性で固定する。月40万円、通常20〜30万円、dry powder、集中warningは人間判断用の目安であり、投資価値順位を変えない。保有・予約銘柄もhard除外せず、買増し・既存注文との関係をannotationする。

## Trigger table

| trigger | 開始条件 | 必須成果物 | 始めないこと |
| --- | --- | --- | --- |
| `opportunity` | 割安候補を探す、週次確認、指値提案 | operation Issue、8〜10候補shortlist reportと人間選択、選択銘柄の一次リサーチ、必要ならpacket/review、proposal | 年次calibration、全保有review |
| `pending-result` | 人間からopen/filled/cancelled報告 | validated ledger draft/差分 | broker状態の推定、screening |
| `monthly-contribution` | 人間が入金を確定 | contribution eventとsnapshot | 購入の強制 |
| `earnings-material-event` | 決算、修正、資本政策等 | 対象tickerのpacket/holding review delta | 全portfolio再調査 |
| `annual-outcome` | 年次評価日 | portfolio outcomeとTOPIX比較 | 短期成績によるpolicy変更 |
| `improvement` | 方法変更のIssue | improvement loopへのhandoff | 個別銘柄判断との混在 |

共有data refreshは1回だけ再利用できるが、成果物と完了条件はtriggerごとに分ける。変更なし、候補0件、deferは正常終了である。

<a id="resume-checkpoint"></a>

## Resume checkpoint

1. `git status --short --branch`でbranchとtracked差分を確認する。dirtyなら所有者と目的を理解するまでrecordを更新しない。
2. triggerを1件選び、対応するoperation Issueへ同じsessionのcheckpointを集約する。
3. `UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-position ledger`でcanonical holdings、active reservations、cash、warningsを読む。warningはannotationでありrankingを変更しない。`event_annotations`のmigration eventはcanonical stateの初期化記録で、人間報告後のbroker resultではないため、当月の新規注文・約定件数へ数えない。
4. このtriggerで使うpublic commandの`--help`とrequired inputを確認する。

全trigger共通のstop条件はdirty worktreeの所有不明、schema/public CLI不明、入力同士の矛盾である。market/EDINET/JPX coverageとmacro freshnessは`opportunity`、価格を再計算するholding/outcome等、そのdataを実際に使うpathだけで確認する。`pending-result`は人間報告、proposal reference、ledger source hash/reconciliationだけで完了でき、market cacheやmacroが不足していても止めない。stop時はcommand、error、判断への影響、必要な入力をIssueへ残す。

`UV_CACHE_DIR=/tmp/uv-cache`はworkspace外のread-only cacheを避け、同じrepository operationを再現するための標準prefixである。

<a id="opportunity-path"></a>

## Opportunity path

以下で`ASOF=最新完全営業日`、`TARGET=次の発注対象session`、`WORKSPACE=.cache/opportunity/$ASOF`と読み替える。shell変数の設定を要求せず、実行時は値を日付へ置換する。

### OP1 Cache coverage

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-screening verify-cache-coverage --asof YYYY-MM-DD
```

- pass: exit 0、必要sourceがASOFをcoverする。
- stop: missing/stale/future data。後続を実行しない。
- refreshが必要な場合だけ次を実行し、再度coverageを単独確認する。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-screening bootstrap-cache --asof YYYY-MM-DD
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-screening extract-edinet-metrics --asof YYYY-MM-DD
```

### OP2 Screening and audit pool

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-screening run --asof YYYY-MM-DD --output-path /tmp/candidates-YYYY-MM-DD.yaml
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-screening select --asof YYYY-MM-DD --candidates /tmp/candidates-YYYY-MM-DD.yaml --detail full --audit-top 20 --output-path /tmp/selection-YYYY-MM-DD.yaml
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-opportunity prepare --asof YYYY-MM-DD --selection-output /tmp/selection-YYYY-MM-DD.yaml --ledger records/04-position/portfolio-ledger.yaml --workspace .cache/opportunity/YYYY-MM-DD
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-opportunity status --workspace .cache/opportunity/YYYY-MM-DD
```

確認するものは`audit_pool`最大20件、production `recommendations`、holding/reservation annotation、workspace hash、`next_command`。candidate/audit poolは探索用で、buy候補やcanonical judgmentではない。

### OP3 Candidate shortlist report (human review gate)

audit pool上位20件から8〜10候補を選び、候補shortlistレポートを人間へ提示する。第1層データ（価格・valuation・自己資本比率・net cash・配当basis・機械E[r]・FVアンカー乖離・TradingView link）と選定背景（なぜ安い / 一時的か構造的か / 5年耐性 / 最強countercase / 深掘り論点）、比較表、非選択理由を含める。数値はscreening出力から機械生成し、narrativeだけ運用者が`narratives.yaml`に書く。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m tools.candidate_report.render --selection .cache/opportunity/YYYY-MM-DD/selection-output.yaml --candidates .cache/opportunity/YYYY-MM-DD/candidates.yaml --narratives .cache/opportunity/YYYY-MM-DD/narratives.yaml --out .cache/opportunity/YYYY-MM-DD/candidate-report.html
```

生成HTMLは`.cache`のephemeral成果物でcommitしない。詳細は[`../reference/candidate-report.md`](../reference/candidate-report.md)。非選択上位候補にも構造的衰退、永久損失warning、一次情報不足、FV乖離不足、投資対象外等の理由を残す。「保有済み」「予約中」「予算外」だけを除外理由にしない。

このレポートを提示し、人間が深掘り対象（推奨2〜4件）を選ぶまで一次リサーチへ進まない。買う候補が無ければこの段階で`no actionable bargain`終了できる。

### OP4 Primary research on selected candidates

人間が選んだ銘柄だけを対象に、会社IR、TDnet、EDINET、JPXを直接確認し、永久損失7軸、corporate action、bear/base/bullの3年sanityと5年total return、FV、countercaseを同じ表で比較する。手順は[`../workflow/research.md`](../workflow/research.md)を正本とする。selectedは0または1件。0件なら`no actionable bargain`で終了する。

### OP5 Packet scaffold

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-opportunity packet-scaffold --workspace .cache/opportunity/YYYY-MM-DD --ticker XXXX --sqlite-path data/screening/market.sqlite --target-session YYYY-MM-DD
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-opportunity status --workspace .cache/opportunity/YYYY-MM-DD
```

生成された`research-checklist.yaml`を一次sourceで`complete / blocked`にする。blockedを推定で埋めない。packetのobserved/derived/estimate/judgmentを区別し、scenario算術を機械再計算する。

### OP6 Independent review and promotion

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-opportunity review-scaffold --workspace .cache/opportunity/YYYY-MM-DD --ticker XXXX
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-opportunity status --workspace .cache/opportunity/YYYY-MM-DD
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-opportunity promote --workspace .cache/opportunity/YYYY-MM-DD --ticker XXXX --output-dir records/03-thesis/YYYY/MM
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-validation --target decision-packet
```

reviewはpacket authorと別roleが行う。`proposal_changed=true`ならpacketへ戻り、packet hash変更後の旧reviewを使わない。promotionはchecklist、packet/review schema、hash、pathが一致するときだけ行う。

### OP7 Planning-only limit

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-opportunity plan-limit --packet records/03-thesis/YYYY/MM/YYYY-MM-DD-XXXX-decision.yaml --ledger records/04-position/portfolio-ledger.yaml --sqlite-path data/screening/market.sqlite --target-session YYYY-MM-DD --budget-min-yen 200000 --budget-max-yen 300000 --output .cache/opportunity/YYYY-MM-DD/XXXX/proposal.yaml
```

`planned_limit`は前営業日終値、max acceptable price、board lotから作る。終値が上限超過、corporate action unresolved、packet/review not readyは`defer`。1単元が30万円を超えても自動棄却せず、超過をwarningとして表示する。

proposal第1層にはticker/name/as-of、5年base CAGR、永久損失結論、最強countercase、max price、limit、quantity/notional、portfolio warnings、packet/review参照、人間に求める`approve / defer / reject`だけを置く。tickerにはTradingView linkを付け、focus tickerだけ開く。AIは発注しない。

<a id="human-result-path"></a>

## Human result path

人間報告をbroker factの唯一の入力とする。

| report | 必須情報 | action |
| --- | --- | --- |
| open | ticker、quantity、limit、expiry、sector、proposal/approval URL | reservation draft |
| filled | ticker、quantity、price、executed_at、proposal/approval URL | execution draft。reservationなしはapproval/guard/expiry/sectorも確認 |
| cancelled | reservation_id、cancelled_at、proposal/approval URL | remaining release draft |

例: open。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-position record-result --ledger records/04-position/portfolio-ledger.yaml --proposal-ref https://github.com/OWNER/REPO/issues/NNN#issuecomment-NNN --status open --occurred-at YYYY-MM-DDTHH:MM:SS+09:00 --ticker XXXX --quantity 100 --sector SECTOR --price-guard-yen 1000 --expires-at YYYY-MM-DDT15:30:00+09:00 --out .cache/ledger/YYYY-MM-DDTHHMMSS-XXXX-open-ledger.yaml
```

例: filled（active reservationあり）。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-position record-result --ledger records/04-position/portfolio-ledger.yaml --proposal-ref https://github.com/OWNER/REPO/issues/NNN#issuecomment-NNN --status filled --occurred-at YYYY-MM-DDTHH:MM:SS+09:00 --ticker XXXX --quantity 100 --price-yen 990 --reservation-id RESERVATION_ID --out .cache/ledger/YYYY-MM-DDTHHMMSS-XXXX-filled-ledger.yaml
```

例: cancelled。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-position record-result --ledger records/04-position/portfolio-ledger.yaml --proposal-ref https://github.com/OWNER/REPO/issues/NNN#issuecomment-NNN --status cancelled --occurred-at YYYY-MM-DDTHH:MM:SS+09:00 --reservation-id RESERVATION_ID --out .cache/ledger/YYYY-MM-DDTHHMMSS-XXXX-cancelled-ledger.yaml
```

`YYYY-MM-DDTHHMMSS`は報告時刻、`XXXX`はticker（cancelledではreservationのticker）へ置換する。各reportで別file名を使い、既存draftを再利用しない。同一reportを再実行してcanonicalに既に同じeventがある場合、CLIは既存`--out`より先にidempotencyを確認し、fileを書かず`status: no_change`を返す。

`record-result`はcanonical ledgerを直接変更せず、実際にparseした同一bytesの`source_ledger_sha256`と新規event IDをstdoutへ返す。`--out`はrepository root配下の相対pathだけを許し、既存fileとsymlinkを上書きしない。報告がなければ何も更新しない。

### Result draftのInspect / Validate / Apply

次の手順を省略しない。`SOURCE_SHA256_FROM_OUTPUT`は`record-result`のstdoutに出た64桁値へ置換する。

1. sourceがdraft生成後に変わっていないことを確認する。

```bash
sha256sum records/04-position/portfolio-ledger.yaml
```

- pass: 表示hashが`SOURCE_SHA256_FROM_OUTPUT`と完全一致。
- stop: 不一致。draftを捨て、現在のcanonical ledgerから`record-result`を再実行する。

2. canonicalとの差分と、draftを再生したsnapshotを確認する。

```bash
git diff --no-index -- records/04-position/portfolio-ledger.yaml .cache/ledger/UNIQUE-RESULT-ledger.yaml
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-position ledger --ledger .cache/ledger/UNIQUE-RESULT-ledger.yaml
```

`git diff --no-index`のexit 1は「期待した差分あり」を表し、この場合は失敗ではない。追加されたeventが人間報告と一致し、既存eventの削除・改変がなく、cash、reservation、holding quantityが説明可能ならpass。不明な差分、未来時刻、proposal不一致、reconciliation errorはstopして人間へ質問する。

3. AIは次をoperation Issueへ記録し、人間にcanonical反映の確認を求める。

```markdown
- human report: <open|filled|cancelled and supplied facts>
- source ledger sha256: <64 hex>
- draft: .cache/ledger/UNIQUE-RESULT-ledger.yaml
- event IDs: <stdout values>
- snapshot delta: <cash/reservation/holding delta>
- confirmation requested: canonical ledgerへこのdraftを反映してよいか
```

4. 人間が明示的に反映を確認した場合だけ、source hashをもう一度確認してcanonicalへcopyする。

```bash
sha256sum records/04-position/portfolio-ledger.yaml
cp .cache/ledger/UNIQUE-RESULT-ledger.yaml records/04-position/portfolio-ledger.yaml
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-validation --target ledger
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-position ledger
git diff -- records/04-position/portfolio-ledger.yaml
```

最初のhashが保存値と違えば`cp`を実行しない。copy後はvalidation、snapshot、最終diffの3つがpassするまでcommitしない。この一人運用ではledger反映を単純な人間確認付きcopyに保ち、別系統の注文状態記録や自動broker照合を設けない。

## Monthly contribution path

人間が入金を確定した後だけ、一意な`contribution` eventをledger draftへ追加する。snapshotとvalidationを確認し、購入やscreeningを強制しない。お買い得候補がなければcashに残す。

<a id="earnings-and-material-event-path"></a>

## Earnings and material-event path

1. [`task-runbook.md`](./task-runbook.md)のdated Issueと対象tickerを確認する。
2. packet以後の一次IRとmaterial deltaだけを調べる。
3. 対象tickerのcurrent decision packet/reviewを更新する。
4. `holding-review-build`でreview draftをsourceから作る。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-position holding-review-build --packet records/03-thesis/YYYY/MM/YYYY-MM-DD-XXXX-decision.yaml --ledger records/04-position/portfolio-ledger.yaml --position-id POSITION_ID --out .cache/holding-review/YYYY-MM-DD-XXXX-attempt-N-review.yaml
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-position holding-review --root . --input .cache/holding-review/YYYY-MM-DD-XXXX-attempt-N-review.yaml
```

`holding-review`はsource hashだけでなくpacket/ledgerからload-bearing scalarを再構築してdraftと照合する。pass後、生成された`action`、最強countercase、source日付をoperation Issueへ要約し、人間に保存確認を求める。確認後だけ次を実行する。`CANONICAL_REVIEW.yaml`は対象positionの既存命名規則に従う新規pathへ置換する。

```bash
mkdir -p records/04-position/YYYY/MM
cp .cache/holding-review/YYYY-MM-DD-XXXX-attempt-N-review.yaml records/04-position/YYYY/MM/CANONICAL_REVIEW.yaml
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-validation --target holding-review
git diff -- records/04-position/YYYY/MM/CANONICAL_REVIEW.yaml
```

canonical pathが既に存在する、sourceが変わった、人間確認がない場合はcopyせず、sourceからdraftを作り直す。

価格下落だけでは売らない。thesis break、永久損失、現値起点5年期待値、税・費用控除後の代替価値から`hold / add / reduce / exit`を提案する。FV到達はreview triggerであり自動売却ではない。

<a id="annual-outcome-path"></a>

## Annual outcome path

portfolio outcomeはcanonical ledger、JPX営業日close、配当込みTOPIX観測から再計算する。期間・source・cash-flow basis不足は`unresolved`。税・費用込みportfolio returnと同期間benchmarkを比較するが、短期結果だけでpolicyを変更しない。見積り方法の変更は[`improvement-loop.md`](./improvement-loop.md)へ渡す。

<a id="operation-issue-log"></a>

## Operation Issue log

各checkpointは次で統一し、全stdoutや長い思考を貼らない。

```markdown
## CP<n> <name> — <YYYY-MM-DD HH:MM JST>

- commit: <sha>
- input: <paths / source dates>
- commands: <public command names>
- result: pass | defer | stop
- evidence: <output path/hash、primary source URL>
- decision: <1〜3 lines>
- next: <next CP or stop reason>
```

候補shortlist checkpointだけ8〜10件の比較表と非選択理由を追加する。recordsへ保存した判断はpath/hashと1〜3行結果だけ参照し、本文をIssueへ複製しない。

## Failure / stop conditions

- public `--help`とrunbookのcommandが一致しない。
- coverage、corporate action、価格basis、load-bearing sourceがunresolved。
- packet/review hashが不一致。
- 人間報告に必要fieldまたはproposal/approval URLがない。
- canonical input hashがdraft作成後に変わった。

## Validation

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-validation
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-position ledger
```

## Related

- [`../workflow/screening.md`](../workflow/screening.md)
- [`../workflow/research.md`](../workflow/research.md)
- [`../workflow/position.md`](../workflow/position.md)
- [`../reference/decision-packet.md`](../reference/decision-packet.md)
- [`../reference/portfolio-ledger.md`](../reference/portfolio-ledger.md)
