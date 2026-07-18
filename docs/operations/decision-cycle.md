---
title: "Continuous decision cycle runbook"
summary: "前営業日終値の候補・指値提案、人間報告後のledger、保有review、年次評価をtrigger別に進める唯一のe2e入口。"
doc_type: operation
status: active
last_reviewed: 2026-07-15
related_docs:
  - "../doctrine.md"
  - "../portfolio-management.md"
  - "../workflow/README.md"
  - "../reference/decision-packet.md"
---

# Continuous decision cycle

Baibai-Loopの日常運用は「最もお買い得な日本株を見つけ、人間が発注判断できる状態にする」ことを目的とする。AIは観測、候補抽出、一次情報確認、5年評価、独立反証、最大許容価格、指値・数量、保有見直しを担当する。人間だけが`approve / defer / reject`とbroker操作を行う。

`opportunity`では一次リサーチの前に人間レビューgateを置く。audit poolから8〜10候補のhuman-review shortlist reportを提示し、人間がprimary-research setを選んでから、選択銘柄だけを個別リサーチする（OP3→OP4）。1銘柄へ先に決め打ちせず、比較可能な候補群を先に人間へ渡す。人間が複数銘柄を選んだ場合はticker別laneでresearchを並行できるが、資本予約は最新canonical ledgerを使って1件ずつ直列に進める。

価格判断にはJPX基盤の最新完全営業日のraw/unadjusted closeを使う。寄り前のrealtime quoteと板は必須入力ではない。AIは約定可能性や当日価格方向を予測しない。人間からbroker結果が報告されるまで注文状態を推定せず、ledgerを更新しない。

候補比較順は、(1)永久的資本毀損リスク、(2)5年期待総合returnとFV乖離、(3)repository portfolioへの追加価値、(4)購入可能性で固定する。追加資金と通常注文額のplanning baselineは[`portfolio-management`](../portfolio-management.md#capital-guidance)を正本とする。dry powderと集中warningは人間判断用の目安であり、投資価値順位を変えない。保有・予約銘柄もhard除外せず、買増し・既存注文との関係をannotationする。

## Trigger table

| trigger | 開始条件 | 必須成果物 | 始めないこと |
| --- | --- | --- | --- |
| `opportunity` | 割安候補を探す、週次確認、指値提案 | operation Issue、8〜10候補human-review shortlist reportとprimary-research setの人間選択、選択銘柄の一次リサーチ、必要ならpacket/review、proposal | 年次calibration、全保有review |
| `pending-result` | 人間からopen/filled/cancelled/expired報告 | validated ledger draft/差分 | broker状態の推定、screening |
| `monthly-contribution` | 人間が入金を確定 | contribution eventとsnapshot | 購入の強制 |
| `earnings-material-event` | 決算、修正、資本政策等 | 対象tickerのpacket/holding review delta | 全portfolio再調査 |
| `annual-outcome` | 年次評価日 | portfolio outcomeとTOPIX比較 | 短期成績によるpolicy変更 |
| `improvement` | 方法変更のIssue | improvement loopへのhandoff | 個別銘柄判断との混在 |

共有data refreshは1回だけ再利用できるが、成果物と完了条件はtriggerごとに分ける。変更なし、候補0件、deferは正常終了である。

<a id="resume-checkpoint"></a>

## Resume checkpoint

1. `git status --short --branch`でbranchとtracked差分を確認する。dirtyなら所有者と目的を理解するまでrecordを更新しない。
2. `UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-position ledger`でcanonical holdings、active reservations、cash、warningsを読む。warningはannotationでありrankingを変更しない。`event_annotations`のmigration eventはcanonical stateの初期化記録で、人間報告後のbroker resultではないため、当月の新規注文・約定件数へ数えない。
3. [`records/05-task/tasks.yaml`](../../records/05-task/tasks.yaml) の open task を due date 順に一覧し、[`task-runbook.md`](./task-runbook.md#resume-checkpoint)に従って current question、expected destination、close condition を canonical records と ledger へ照合する。task、canonical record、ledger が矛盾する場合は推定で進めない。
4. triggerを1件選び、対応するoperation Issueへ同じsessionのcheckpointを集約する。
5. このtriggerで使うpublic commandの`--help`とrequired inputを確認する。

全trigger共通のstop条件はdirty worktreeの所有不明、schema/public CLI不明、入力同士の矛盾である。task、canonical record、ledgerの矛盾も同じstop条件として扱う。market/EDINET/JPX coverageとmacro freshnessは`opportunity`、価格を再計算するholding/outcome等、そのdataを実際に使うpathだけで確認する。`pending-result`は人間報告、proposal reference、ledger source hash/reconciliationだけで完了でき、market cacheやmacroが不足していても止めない。人間のbroker結果報告がない限り、期日経過や他入力の欠落から`filled / cancelled / expired`を推定せず、task、records、ledgerを終端状態へ進めない。stop時はcommand、error、判断への影響、必要な入力をoperation Issueへ残す。

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

確認するものは`audit_pool`最大20件、production `recommendations`、holding/reservation annotation、workspace hash、`next_command`。`recommendations`はrulesの通常表示capを適用した機械出力で、OP3のhuman-review shortlistではない。candidate/audit poolは探索用で、buy候補やcanonical judgmentではない。

### OP2.5 Promoted research price watch

weekly runではpromote済みresearchのFVとASOFのraw closeをread-onlyで比較し、stdout YAMLをshortlist checkpointへ貼ってOP3 reportと同時に提示する。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m tools.research_price_watch --packets-root records/03-thesis --ledger records/04-position/portfolio-ledger.yaml --sqlite-path data/screening/market.sqlite --asof YYYY-MM-DD
```

これはscreening top-20外の再調査候補を見つける観測であり、過去FV・recommendationを現在のbuy signalにしない。`market_asof`は価格時点、`ledger_as_of`はcurrent portfolio annotationの時点として別に読む。`re_research_required: true`のtickerを調べ直す場合は、新しいopportunityまたは[`earnings-and-material-event` path](#earnings-and-material-event-path)で一次情報・FV・countercaseを更新してから提案する。

### OP3 Candidate shortlist report (human review gate)

audit pool上位20件から8〜10候補を選び、human-review shortlist reportを人間へ提示する。audit poolが8件未満なら全件を提示して不足を明記し、pool外の銘柄で件数を埋めない。第1層データ（価格・valuation・財務・配当basis・機械E[r]とreversion/carry分解・FVアンカー構成と乖離・値位置・流動性・次回決算日・データ品質flag・portfolio annotation・TradingView link。field一覧は[`../reference/candidate-report.md`](../reference/candidate-report.md)を正本とする）と選定背景（なぜ安い / 一時的か構造的か / 5年耐性 / 最強countercase / 深掘り論点）、比較表、非選択理由を含める。数値はscreening出力とprepare出力から機械生成し、narrativeだけ運用者が`narratives.yaml`に書く。

`new`の候補（前回reportを確認できないfull reportでは全候補）は、narrativeを書く前に会社IR・TDnetの直近開示をタイトルレベルで確認する。screeningのas-of財務に反映されないmaterial開示（業績修正、資本政策、TOB/MBO、不祥事等）があれば`why`と`counter`へ反映し、確認した開示範囲をshortlist checkpointへ残す。この確認はresearch laneを割く前の開示スキャンであり、一次リサーチの代替ではない。

直近の前回reportがある週次runでは、前回と今回のhuman-review shortlistをtickerで比較し、`new / continued / exited`をoperation Issueのshortlist checkpointに残して今回reportと同時に人間へ提示する。`new`は今回だけ、`continued`は両方、`exited`は前回だけに含まれるtickerとする。`new`には今回shortlistへ入れる理由、`exited`には今回shortlistへ残さない理由を新たに書く。前回reportを確認できないrunは差分を推定せず、全候補のnarrativeを確認するfull reportへfallbackする。

`continued`のnarrativeは、前回からのaudit-pool順位差、価格、前回as-of後に会社IR・TDnet・EDINETで公表された最新開示、最強countercaseと判断を変え得るmaterial deltaを確認し、現在も判断を支える場合だけ再利用できる。いずれかにmaterial changeまたは確認不能があれば該当narrativeを更新し、差分理由をcheckpointへ残す。今回reportの価格、valuation、E[r]、FV等の数値は必ず今回runのscreening出力からrendererで生成し、前回reportや前回`narratives.yaml`から転記または再利用しない。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m tools.candidate_report.render --selection .cache/opportunity/YYYY-MM-DD/selection-output.yaml --candidates .cache/opportunity/YYYY-MM-DD/candidates.yaml --narratives .cache/opportunity/YYYY-MM-DD/narratives.yaml --prepared .cache/opportunity/YYYY-MM-DD/selection.yaml --out .cache/opportunity/YYYY-MM-DD/candidate-report.html
```

生成HTMLは`.cache`のephemeral成果物でcommitしない。詳細は[`../reference/candidate-report.md`](../reference/candidate-report.md)。非選択上位候補にも構造的衰退、永久損失warning、一次情報不足、FV乖離不足、投資対象外等の理由を残す。「保有済み」「予約中」「予算外」だけを除外理由にしない。weekly差分の分類と確認結果は当面operation Issueへ残し、2〜3回の運用テストでfieldと表示の必要性が安定するまでrenderer/schemaへ組み込まない。

このレポートを提示し、人間がprimary-research set（推奨2〜4件）を選ぶまで一次リサーチへ進まない。選択結果はworkspaceの`selection.yaml.shortlist`へ記録し、件数上限はselection outputの`research_selection_target_max`に従う。買う候補が無ければこの段階で`no actionable bargain`終了できる。

### OP4 Primary research on selected candidates

人間が選んだprimary-research setだけを対象に、会社IR、TDnet、EDINET、JPXを直接確認する。複数銘柄が選ばれたことが並行researchのtriggerであり、暴落検知やmacro timing判定を自動追加しない。

共有workspaceの`manifest.yaml`に固定したselection output / ledger hashを全laneの共通lineageとし、各tickerの成果物は`.cache/opportunity/YYYY-MM-DD/<ticker>/`へ隔離する。永久損失7軸、corporate action、bear/base/bullの3年sanityと5年total return、FV、countercaseの調査はlaneごとに並行できる。手順は[`../workflow/research.md`](../workflow/research.md)を正本とする。

### OP5 Per-lane packet and research scaffold

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-opportunity packet-scaffold --workspace .cache/opportunity/YYYY-MM-DD --ticker XXXX --sqlite-path data/screening/market.sqlite --target-session YYYY-MM-DD
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-opportunity status --workspace .cache/opportunity/YYYY-MM-DD
```

`packet-scaffold`は`selection.yaml.shortlist`に含まれるtickerだけを受け入れる。primary-research setの各tickerについて実行し、生成されたlane内の`research-checklist.yaml`を一次sourceで`complete / blocked`にする。screening E[r]とFV baselineはestimateとしてselection snapshotから機械転記し、observed factへ変換しない。blockedを推定で埋めない。packetのobserved/derived/estimate/judgmentを区別し、scenario算術を機械再計算する。他laneのdraftをcopyまたは上書きしない。

### OP6 Independent review and promotion

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-decision .cache/opportunity/YYYY-MM-DD/XXXX/packet-draft.yaml
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-opportunity review-scaffold --workspace .cache/opportunity/YYYY-MM-DD --ticker XXXX
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-opportunity status --workspace .cache/opportunity/YYYY-MM-DD
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-opportunity promote --workspace .cache/opportunity/YYYY-MM-DD --ticker XXXX --output-dir records/03-thesis/YYYY/MM
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-validation --target decision-packet
```

reviewはpacket authorと別roleがlaneごとに行い、複数laneを並行できる。reviewerは最初のcommandが返す5年base break-evenと観測multipleを、[`workflow/research.md#independent-review`](../workflow/research.md#independent-review)のfield対応で既存research checklistへ記録してからreview draftを完成させる。`proposal_changed=true`なら該当laneのpacketへ戻り、packet hash変更後の旧reviewを使わない。

全laneの調査・反証後に共有`research-comparison.yaml`で比較し、現在の提案roundの最良0〜1件だけを`selected_ticker`へ固定する。0件なら`no actionable bargain`で終了する。promotionは`selected_ticker`のlaneについて、checklist、packet/review schema、hash、pathが一致するときだけ行う。

### OP7 Planning-only limit

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-opportunity plan-limit --packet records/03-thesis/YYYY/MM/YYYY-MM-DD-XXXX-decision.yaml --ledger records/04-position/portfolio-ledger.yaml --sqlite-path data/screening/market.sqlite --target-session YYYY-MM-DD --budget-min-yen 200000 --budget-max-yen 300000 --output .cache/opportunity/YYYY-MM-DD/XXXX/proposal.yaml
```

`planned_limit`は前営業日終値、max acceptable price、board lotから作る。終値が上限超過、corporate action unresolved、packet/review not readyは`defer`。1単元が30万円を超えても自動棄却せず、超過をwarningとして表示する。同一tickerのactive reservationがある場合は、元注文の再表示と追加注文を機械的に区別できないため`active_reservation_exists`で`defer`する。human resultで約定またはreleaseをledgerに反映するまで新規注文を作らない。

`portfolio_exposure`はproposalと同じ`price_as_of`のraw/unadjusted closeで全保有を横断再評価した一時計算である。ticker / sector / common-factorごとに、保有とactive reservationを合わせた現在額、今回注文後のprospective額と比率、warning閾値を確認する。`ledger_fallback_tickers`が空でない場合は`holding_valuation_status: mixed_with_ledger_fallback`であり、欠損またはcorporate action未解決の銘柄だけledger評価額が混在する。`common_factor_empty_tickers`が空でない場合のfactor比率は宣言済みtagに基づく下限値である。どちらのwarningも解消せず人間へ提示する。この再評価はledgerを更新しない。

複数laneがviableでも、同じledger snapshotから複数proposalを一括生成しない。最上位laneを`plan-limit`したら、出力の`source_ledger_sha256`が現在のcanonical ledgerと一致することを確認して人間へ提示する。人間の`approve / defer / reject`と、注文がある場合はhuman result pathによるcanonical ledger更新を完了してから、残るlaneを再比較する。次のlaneへ進む場合は`selected_ticker`をその1件へ更新し、更新後canonical ledgerで`plan-limit`を再実行する。これによりactive reservationを含まないstale ledgerから資本を二重に割り当てない。

proposal第1層にはticker/name/as-of、5年base CAGR、永久損失結論、最強countercase、max price、limit、quantity/notional、portfolio warnings、packet/review参照、人間に求める`approve / defer / reject`だけを置く。TradingView linkはOP9の統合HTMLへ各ticker分を生成し、browserを自動起動しない。AIは発注しない。

### OP8 Integrated research content review

全laneの詳細調査、比較、購入方法を`findings.yaml`へ統合する。定性contentだけをfindingsへ書き、source/fact/scenario/FV/7軸はlane packet、採否は`research-comparison.yaml`、価格・数量・notionalは`proposal.yaml`を正本として転記しない。共通field、業種固有分析、海外展開3層、growth qualityの分解は[`../reference/research-decision-report.md`](../reference/research-decision-report.md)を正本とする。

HTMLをreviewしない。report compilerとは別roleが、軽量なfindings / comparison / packet / proposalをreviewするため、hash-bound draftを作る。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m tools.research_decision_report.review_scaffold --workspace .cache/opportunity/YYYY-MM-DD --findings .cache/opportunity/YYYY-MM-DD/findings.yaml --proposal .cache/opportunity/YYYY-MM-DD/XXXX/proposal.yaml --out .cache/opportunity/YYYY-MM-DD/report-review-attempt-1.yaml
```

reviewerはsource freshness、指定質問、一次source、fact/estimate分離、countercase/unknown、scenario/FV、比較/portfolio fit、購入方法を再確認する。`changes_required`なら該当入力へ戻り、変更後は既存reviewを上書きせず`report-review-attempt-2.yaml`のようにattempt番号を増やして再scaffoldする。全checkが`pass`で入力hashが一致するときだけHTMLへ進む。`selected_ticker: null`ではproposalを省略し、購入提案なしの結論をreviewする。

### OP9 Reviewed HTML and human checkpoint

この工程はrepository内の専用rendererを正本とする。汎用HTML生成skillや別templateを使わない。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m tools.research_decision_report.render --workspace .cache/opportunity/YYYY-MM-DD --findings .cache/opportunity/YYYY-MM-DD/findings.yaml --review .cache/opportunity/YYYY-MM-DD/report-review-attempt-N.yaml --proposal .cache/opportunity/YYYY-MM-DD/XXXX/proposal.yaml --out .cache/opportunity/YYYY-MM-DD/research-decision-report.html
```

HTMLはreview済み入力のephemeral projectionであり、内容reviewやcanonical recordの対象にしない。plan-limitが`defer`、または比較結果が`no actionable bargain`でも正常に生成し、注文なしと理由を明示する。repository visibilityを確認してから、operation Issueへmanifest、findings、comparison、non-promoted lane packet、proposal、report reviewのreview済み内容をartifact別commentとして保存する。promote済みselected packet/reviewはcanonical pathとhashを参照し、同じ内容を複製しない。summaryに各comment URLまたはcanonical pathとhash、全laneの採否、購入方法または注文なしの理由、HTML pathを残す。local pathとhashだけを残してcompact inputを破棄しない。人間の`approve / defer / reject`を待つ。

### Opportunity Issueのcloseと注文監視の移管

review済みの提案または注文なし結論と、人間の`approve / defer / reject`を記録した時点で、opportunity triggerの判断責務は完了する。

- `defer / reject`またはbrokerの注文報告がない場合は、判断と理由をcommentして元Issueをcloseする。`approve`だけからbroker factやreservationを作らない。
- 人間の`open`報告またはpartial fillでremaining reservationがある場合、同一reservationを追跡するdated Issueを1件作成または再利用する。canonical ledgerへ未反映の間は`blocked`、active reservation反映後は`living`とする。
- 移管先にreservation ID、expiry、元proposal/approval comment URLを記録し、元Issueと相互linkする。元Issueがopenなら移管後にcloseし、既にclose済みなら移管先URLを追記する。
- 後続の`filled / cancelled / expired`でも元proposal/approval URLをledgerのdecision referenceに使い、ledger event本文や全履歴を複製せず、Human result path所定のcheckpointだけを移管先に記録する。
- partial fillはremaining reservationがある間`living`とし、full fill、cancelled、expiredはcanonical execution/release反映後に移管先を`complete`とする。期限超過だけで状態を推定しない。
- active reservationが残らない即時full fillは監視Issueへ移管せず、Human result pathのcheckpointとcanonical execution反映で完了する。

<a id="human-result-path"></a>

## Human result path

人間報告をbroker factの唯一の入力とする。

| report | 必須情報 | action |
| --- | --- | --- |
| open | ticker、quantity、limit、expiry、sector、proposal/approval URL | reservation draft |
| filled | ticker、quantity、price、executed_at、proposal/approval URL | execution draft。reservationなしはapproval/guard/expiry/sectorも確認 |
| cancelled | reservation_id、cancelled_at、proposal/approval URL | remaining release draft |
| expired | reservation_id、expired_at、proposal/approval URL | remaining release draft。reservation_idを省略せず、`expired_at >= expires_at`を必須とする |

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

例: expired。brokerで未約定のまま期限到来したことを人間が確認してから実行し、時刻や状態を自動推定しない。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-position record-result --ledger records/04-position/portfolio-ledger.yaml --proposal-ref https://github.com/OWNER/REPO/issues/NNN#issuecomment-NNN --status expired --occurred-at YYYY-MM-DDTHH:MM:SS+09:00 --reservation-id RESERVATION_ID --out .cache/ledger/YYYY-MM-DDTHHMMSS-XXXX-expired-ledger.yaml
```

`YYYY-MM-DDTHHMMSS`は報告時刻、`XXXX`はticker（cancelled/expiredではreservationのticker）へ置換する。各reportで別file名を使い、既存draftを再利用しない。同一reportを再実行してcanonicalに既に同じeventがある場合、CLIは既存`--out`より先にidempotencyを確認し、fileを書かず`status: no_change`を返す。

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

`filled`が新規holdingを作り、そのtickerのmarket priceがまだ無い場合だけ、中間result draftは`market price is required for holding`でsnapshotを生成できない。この中間状態をcanonicalへcopyせず、event差分を確認したうえで価格なしevent replayを使う`market-price-draft`へ直接渡す。

```bash
sha256sum .cache/ledger/UNIQUE-FILLED-ledger.yaml
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-position market-price-draft --root . --ledger .cache/ledger/UNIQUE-FILLED-ledger.yaml --sqlite data/screening/market.sqlite --asof YYYY-MM-DD --out .cache/position/YYYY-MM-DD-market-price-after-fill-ledger.yaml
sha256sum .cache/position/YYYY-MM-DD-market-price-after-fill-ledger.yaml
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-position ledger --ledger .cache/position/YYYY-MM-DD-market-price-after-fill-ledger.yaml
```

このcompositionでは、(1) current canonical hashと`record-result`の`source_ledger_sha256`、(2) filled中間draftのbyte hashと`market-price-draft`の`source_ledger_sha256`、(3) 最終draftのbyte hashと`draft_sha256`を順に完全一致させる。open holdingは中間ledgerの`as_of`までevent replayして決め、価格は指定した最新完全営業日の同日raw/unadjusted closeを使う。このため価格観測時刻が当日のfillより前でも、eventを巻き戻さず全open holdingを評価できる。価格不足以外のreconciliation error、hash不一致、raw close欠損では停止する。人間確認後にcanonicalへ反映するのは、reconciliationを通った最終draftだけである。

3. AIは次をoperation Issueへ記録し、人間にcanonical反映の確認を求める。

```markdown
- human report: <open|filled|cancelled|expired and supplied facts>
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

### Expired limit feedback

human-confirmed `release(reason=expired)`をcanonical ledgerへ反映した後だけ、未約定残数の機会観測をread-onlyで作る。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m tools.limit_outcome --ledger records/04-position/portfolio-ledger.yaml --reservation-id RESERVATION_ID --sqlite-path data/screening/market.sqlite --asof YYYY-MM-DD
```

stdout YAMLはledger ref/hash、market SQLite ref、同一read-only transactionで実際に観測対象としたcalendar/raw bar rowsのfingerprintとhash basis、reservation、未約定残数、raw/unadjusted daily lowのtouch、期限後5 JPX営業session固定のraw close変化を示す。fingerprintと判定範囲はsubmissionから5 session horizonまでで打ち切り、それより後のbarを混ぜない。submission日はintraday順序が不明なのでtouch判定から除外するが、corporate-action basis確認には含める。expiry日は`expires_at`が15:30 JSTまで有効な場合だけ含める。daily lowが指値以下でも約定とはみなさず、期限後変化はexpiry直前営業session closeからの実価格変化であって、指値約定や逸失利益の推定ではない。

human-confirmed release前、必要な期限後session未到来は`pending`、cancel等の別terminal reasonは`not_eligible`、raw price欠損・calendarとの不整合・corporate action・`adjustment_factor`未確認は`unresolved`とする。ledger event stateがreplayできない場合、または`asof`がsubmission/releaseより前なら停止する。adjusted priceで補完しない。この個票toolはcanonical recordやlocal fileを書かず、stdoutだけを返す。YAMLはoperation Issueへ貼り、初期サンプルを比較する。反復利用と効果を確認するまでは永続schema、aggregate、stable public CLIへ昇格しない。

## Monthly contribution path

人間が入金を確定した後だけ、一意な`contribution` eventをledger draftへ追加する。snapshotとvalidationを確認し、購入やscreeningを強制しない。お買い得候補がなければcashに残す。

<a id="earnings-and-material-event-path"></a>

## Earnings and material-event path

1. [`records/05-task/tasks.yaml`](../../records/05-task/tasks.yaml) の open task と対象 ticker を確認する。
   JPX の決算発表予定は日付確認の補助事実であり、通知や自動 trigger ではない。実施時は一次 IR で発表を確認する。
2. 最新完全営業日の全保有raw closeからledger draftを作り、source hash、全ticker同日、raw/unadjusted basisを確認する。人間が確認したdraftだけをcanonical ledgerへcopyし、ledger validationを通す。
3. canonical ledgerのopen holdingを起点に1銘柄固定workspaceを作る。
4. packet以後の一次IRとmaterial deltaだけを調べ、既存のpacket/review/promote経路で対象tickerのcurrent decision packet/reviewを更新する。
5. `holding-review-build`でreview draftをsourceから作る。

ここでも`ASOF_DATE`は価格draftに使う最新完全営業日、`NEXT_SESSION_DATE`はその次の取引sessionを表す。`packet-scaffold`が解決するraw close日は`ASOF_DATE`と一致しなければならない。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-position market-price-draft --root . --ledger records/04-position/portfolio-ledger.yaml --sqlite data/screening/market.sqlite --asof ASOF_DATE --out .cache/position/ASOF_DATE-market-price-ledger.yaml
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-position ledger --ledger .cache/position/ASOF_DATE-market-price-ledger.yaml
git diff --no-index records/04-position/portfolio-ledger.yaml .cache/position/ASOF_DATE-market-price-ledger.yaml
cp .cache/position/ASOF_DATE-market-price-ledger.yaml records/04-position/portfolio-ledger.yaml
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-validation --target ledger
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-opportunity holding-prepare --asof ASOF_DATE --ledger records/04-position/portfolio-ledger.yaml --ticker XXXX --workspace .cache/opportunity/ASOF_DATE/holding-XXXX
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-opportunity packet-scaffold --workspace .cache/opportunity/ASOF_DATE/holding-XXXX --ticker XXXX --sqlite-path data/screening/market.sqlite --target-session NEXT_SESSION_DATE
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-opportunity review-scaffold --workspace .cache/opportunity/ASOF_DATE/holding-XXXX --ticker XXXX
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-opportunity promote --workspace .cache/opportunity/ASOF_DATE/holding-XXXX --ticker XXXX --output-dir records/03-thesis/YYYY/MM
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-position holding-review-build --packet records/03-thesis/YYYY/MM/ASOF_DATE-XXXX-decision.yaml --ledger records/04-position/portfolio-ledger.yaml --position-id POSITION_ID --out .cache/holding-review/ASOF_DATE-XXXX-attempt-N-review.yaml
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-position holding-review --root . --input .cache/holding-review/ASOF_DATE-XXXX-attempt-N-review.yaml
```

`market-price-draft`は指定日のJ-Quants raw closeが全open holdingで同日に揃い、既存のopen holding price日を巻き戻さない場合だけ新規draftを作る。canonical ledgerを上書きせず、adjusted closeで補完しない。copy直前にstdoutのsource ledger hashが現在のcanonical ledgerと一致し、draftのbyte hashがstdoutの`draft_sha256`と一致することを確認する。どちらかが異なればcopyせず再生成する。market row fingerprintも確認し、人間確認なしにcanonicalへcopyしない。`holding-prepare`はcanonical ledgerに実在し、market-price observationの日付が`--asof`と一致するopen holdingだけを受け入れ、audit pool、shortlist、selected tickerをその1銘柄に固定する。通常のopportunity `prepare`とprimary-research set gateは変更しない。

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

human-review shortlist checkpointだけ8〜10件の比較表と非選択理由を追加する。recordsへ保存した判断はpath/hashと1〜3行結果だけ参照し、本文をIssueへ複製しない。

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
