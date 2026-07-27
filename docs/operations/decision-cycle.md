---
title: "Decision Cycle"
summary: "1 triggerをoperation sessionで進め、proposalとhuman-confirmed ledgerへ接続する運用正本。"
doc_type: runbook
status: active
last_reviewed: 2026-07-23
---

# Decision Cycle

AIは観測・分析・提案・人間報告後の記録を担当し、人間だけが `approve / defer / reject` とbroker操作を行う。application dataは `data/app/baibai.sqlite` が正本であり、GitHub Issueは開発作業にだけ使う。

## Triggerとsession

| trigger | `session_kind` | final record |
| --- | --- | --- |
| 候補抽出・購入機会 | `opportunity` | shortlist比較、research ID、proposal IDまたは見送り理由 |
| 注文・約定・取消・失効の人間報告 | `pending-result` | proposal / reservationと適用したledger event ID |
| 入出金・income・cost・tax | `monthly-contribution` | 適用したevent IDとsnapshot差分 |
| 決算・material event・保有見直し | `earnings-material-event` | thesis / review / holding-review IDと判断 |
| 年次評価 | `annual-outcome` | outcome IDまたはunresolved理由 |
| 方法改善 | `improvement` | improvement-loopへのhandoff |

全kindを通じてactive sessionは最大1件である。retry / resumeは同じrowを使い、完了後の新しいtriggerは新しいrowにする。opportunity後のbroker報告は別の`pending-result` rowである。

<a id="resume-checkpoint"></a>

## Start / resume / complete

1. `git status --short --branch`、open task、current ledgerをread-onlyで確認する。
2. active sessionがあれば同じIDをresumeする。なければ1件だけstartする。
3. current checkpointだけをpayload fileへ書き、`checkpoint`でrow全体を置換する。旧checkpointを複製しない。
4. kindごとのcanonical ID、人間確認結果、`result`、`next`が揃ったらcompleteする。completed rowはimmutableである。

```bash
uv run baibai-engine operation show --status active
uv run baibai-engine position ledger --db data/app/baibai.sqlite
uv run baibai-engine operation start --kind opportunity --as-of YYYY-MM-DD --payload /tmp/operation.yaml
uv run baibai-engine operation checkpoint OPERATION_ID --payload /tmp/operation.yaml
uv run baibai-engine operation complete OPERATION_ID --payload /tmp/operation-final.yaml
```

payloadは`checkpoint`、review済みでcanonical homeを持たない`artifacts`、`canonical_refs`、`human_confirmation`、`handoff`、`result`、`next`だけを持つ。promote済みentityはIDと1〜3行の結果だけ参照する。operation sessionは監査logやevent sourcingではなく、1 triggerのcurrent workspaceとimmutable final recordである。

dirty worktreeの所有不明、public `--help`不明、入力矛盾では停止する。market / macro coverageは実際に使うpathだけで確認する。`pending-result`を無関係なdata不足で止めず、人間報告がないbroker状態を推定しない。

<a id="opportunity-path"></a>

## Opportunity path

1. `screening run`が返す`run_revision_id`を明示して`select --longlist-top 20`する。
2. longlistから[human-review gate](#opportunity-human-review-gate-op3)の件数契約で候補を選び、各selected銘柄へOP3 narrative、各rejected銘柄へ具体的な非選択理由を付けたshortlist draftを、source `selection_id`へ束縛して`screening shortlist publish`する。machine recommendationをshortlistと呼ばない。
3. 人間がレビュー面からprimary-research setを選び、その銘柄のresearch workspaceを作って一次IR、3年/5年scenario、永久損失、FV / E[r]、countercaseを調べる。
4. thesisとindependent reviewを`research promote`し、返された`thesis_id` / `review_id`をsessionから参照する。
5. planning-only limitの出力からproposalを作る。proposalは`pending`で始まり、人間の報告だけを`proposal decide`で記録する。
6. shortlistは`baibai-app`の`/stocks/shortlist`レビュー面、research decision reportはephemeral HTML projectionとして提示し、broker操作へ進まない。proposal IDまたは`no actionable bargain / defer`をfinal resultにしてsessionをcompleteする。

```bash
uv run baibai-engine screening run --asof YYYY-MM-DD
uv run baibai-engine screening select --asof YYYY-MM-DD --run-revision-id RUN_REVISION_ID --longlist-top 20 --output-path /tmp/selection.yaml
uv run baibai-engine screening shortlist publish /tmp/shortlist.yaml
uv run baibai-engine research prepare --asof YYYY-MM-DD --selection-output /tmp/selection.yaml --db data/app/baibai.sqlite --workspace .cache/opportunity/YYYY-MM-DD
uv run baibai-engine research promote --workspace .cache/opportunity/YYYY-MM-DD --ticker XXXX --db data/app/baibai.sqlite
uv run baibai-engine research plan-limit --thesis .cache/opportunity/YYYY-MM-DD/XXXX/thesis-draft.yaml --db data/app/baibai.sqlite --sqlite-path data/screening/market.sqlite --target-session YYYY-MM-DD --output /tmp/proposal-input.yaml
uv run baibai-engine proposal --db data/app/baibai.sqlite --market-db data/screening/market.sqlite create --thesis-id THESIS_ID --input /tmp/proposal-input.yaml
uv run baibai-engine proposal --db data/app/baibai.sqlite --market-db data/screening/market.sqlite decide PROPOSAL_ID --decision approve
```

`approve`時はcurrent DBのthesis、price、quantity、expiry、portfolio constraintを再計算する。不一致ならno-writeで新しいproposalを作る。`defer / reject`も正常な結論である。

<a id="opportunity-human-review-gate-op3"></a>

### Opportunity human-review gate (OP3)

一次リサーチの前に、比較可能な候補群を人間へ渡すレビューgateを置く。1銘柄へ先に決め打ちしない。

- **件数契約**: `longlist`上位20件から8〜10候補をshortlistへ入れる。longlistが8件未満なら全件を提示して不足を明記し、pool外の銘柄で件数を埋めない。`recommendations`のproduction capはこの件数を決めない。
- **判断の記録**: selected銘柄は`ShortlistEntry.narrative`（なぜ安いか / 一時的か / 構造的か / 5年耐性 / unlock / 最強countercase / 深掘り論点 / 深掘り価値 / 暫定判断と暫定`ploss`）を必須にし、rejected銘柄は「順位が低い」「予算外」だけでない具体的理由を必須にする。draftは[`tools/shortlist/draft-template.yaml`](../../tools/shortlist/draft-template.yaml)を写して記入し、`screening shortlist publish`でapplication DBへ一次記録する。narrativeをephemeral HTMLに残さない。
- **再評価triggerの接続**: `screening shortlist publish`は成功時、selected以外（rejected）の各entryについて、束縛したrun candidatesの`next_earnings_date`から`baibai-engine task add --kind follow-up --event-date <決算日> ...`をそのまま実行できる形でstderrへ印字する（決算日が未公表なら手動でtrigger日を決める注記）。stdoutはmachine-readableな公開payloadのままにする。「今は買わない」割安候補のdated re-entry triggerは、この提案からfollow-up taskを起票してBaibai Appのnext_eventへ載せる。write境界は人間に残し、taskをtrigger発火の正本にする。
- **開示スキャン**: narrativeを書く前に、新規候補（前回shortlistを確認できないfull reviewでは全候補）について会社IR・TDnetの直近開示をタイトルレベルで確認し、screeningのas-of財務に反映されないmaterial開示（業績修正、資本政策、TOB/MBO、不祥事等）をnarrativeの`why` / `counter`へ反映する。
- **macro hintの消化**: shortlist作成の前提となるmacro contextは[深度契約](../workflow/macro.md#depth-contract)を満たすものを使う。head の`as_of`が古い、または深度契約を満たさないと判断したら、shortlist作成の前に書き直す。selected銘柄のnarrative `research` / `counter`は、published contextのconnectionセクションにあるresearch優先度ヒント / sizing cautionのうち当該銘柄に該当するものを明示的に消化する（該当なしならその判断を書く）。hintを黙って落とさない。
- **差分確認**: 直近の前回shortlist（application DB）がある週次runでは、今回とticker集合を`new / continued / exited`で比較する。`continued`は前回narrativeを自動継承せず、longlist順位差・価格・最新開示・最強countercaseを再確認したうえでmaterial changeがなければ再利用する。前回を確認できないrunは差分を推定せず全候補を確認する。
- **primary-research set**: `/stocks/shortlist`レビュー面（narrativeとselection longlistのFVアンカー・現値・乖離、candidateのE[r]分解・YoY・流動性・品質flag・portfolio状態を機械join表示）を提示し、人間が深掘り銘柄を選ぶ。推奨2〜4件（hard ruleではない）、上限はselection outputの`research_selection_target_max`。買う候補が無ければこの段階で`no actionable bargain`終了できる。

<a id="human-result-path"></a>

## Human result path

人間報告をbroker factの唯一の入力とする。

| report | required facts | draft effect |
| --- | --- | --- |
| `open` | approved proposal ID、ticker、quantity、limit、expiry、sector、時刻 | reservation |
| `filled` | proposal ID、ticker、quantity、price、時刻。reservation経由ならreservation ID | execution、必要ならremaining reservation |
| `cancelled` | proposal ID、reservation ID、時刻 | remaining release |
| `expired` | proposal ID、reservation ID、brokerで未約定を確認した時刻 | remaining release |

新規openとreservationなしfillはcurrent approved proposalを必須とする。既存reservationのterminal resultはreservationに保存されたproposal bindingを使う。期限経過だけで`expired`を作らない。partial fillはremainingがある間だけ後続reportを受け、同一terminal reportはno-change、矛盾reportはhard errorにする。

```bash
uv run baibai-engine position record-result --db data/app/baibai.sqlite --proposal-ref PROPOSAL_ID --status open --occurred-at YYYY-MM-DDTHH:MM:SS+09:00 --ticker XXXX --quantity 100 --sector SECTOR --price-guard-yen 1000 --expires-at YYYY-MM-DDT15:30:00+09:00 --out /tmp/open-draft.yaml
uv run baibai-engine position record-result --db data/app/baibai.sqlite --proposal-ref PROPOSAL_ID --status filled --occurred-at YYYY-MM-DDTHH:MM:SS+09:00 --ticker XXXX --quantity 100 --price-yen 990 --reservation-id RESERVATION_ID --out /tmp/fill-draft.yaml
uv run baibai-engine position apply-draft /tmp/open-draft.yaml --db data/app/baibai.sqlite --confirmed
```

draft生成はcanonical DBを変更しない。人間がreport、event、cash / reservation / holding差分を確認した後だけ`apply-draft --confirmed`する。applyはexpected append head、proposal / reservation binding、payload、domain invariantを同一transactionで再検証する。staleならno-writeでcurrent DBからdraftを作り直す。

<a id="monthly-contribution-path"></a>

## Cash event path

`contribution / withdrawal / income / cost / tax_confirmed`は人間が確認した事実ごとに1 event draftを作る。購入やscreeningを強制しない。

```bash
uv run baibai-engine position event-draft --type contribution --event-id EVENT_ID --occurred-at YYYY-MM-DDTHH:MM:SS+09:00 --amount-yen 100000 --db data/app/baibai.sqlite --out /tmp/event-draft.yaml
uv run baibai-engine position apply-draft /tmp/event-draft.yaml --db data/app/baibai.sqlite --confirmed
```

risk override、estimated exit tax設定、market priceも各typed draftを作り、同じinspect / human confirmation / apply境界を通す。

<a id="earnings-and-material-event-path"></a>

## Earnings / material event / holding review

1. 対象holdingと最新完全営業日のraw/unadjusted closeを確認し、`market-price-draft`を人間確認後にapplyする。
2. DBのcurrent ledgerとthesisから1銘柄workspaceを作り、一次情報のmaterial deltaだけを更新する。
3. thesis / reviewをpromoteし、DB sourceから`holding-review-build`する。
4. load-bearing scalar、thesis revision、ledger state、`thesis health`、税引後代替価値を検証する。
5. 人間確認後だけ`holding-review publish`し、IDと`hold / add / reduce / exit`をsessionに記録する。
6. `reduce / exit`判定に沿って人間が発注し約定したら、その事実だけを`sell-result-draft`で記録する。builderはcurrent ledgerの保有数量とFIFO原価を検証したexecution(side=sell) draftを作り、人間確認後だけ`apply-draft --confirmed`で反映する。判断元のholding review IDを`--decision-reference`で紐付ける。broker手数料は`--fees-yen`（cost event）、確定した譲渡益税は`--tax-yen`（confirmed_tax event）で同一draftに載せる。指値計画は機械支援せず、人間がholding reviewを見て発注する。約定日が最終market price観測から`market_price_max_age_days`（7日）を超える場合はreconcileがprice stalenessで拒否するため、先に`market-price-draft`を適用する。同時刻・同値の分割約定は1件に合算するか`--occurred-at`を分け、手数料・税が無い場合はフラグを省略する（`0`指定は拒否される）。

```bash
uv run baibai-engine position market-price-draft --db data/app/baibai.sqlite --sqlite data/screening/market.sqlite --asof ASOF_DATE --out /tmp/market-price-draft.yaml
uv run baibai-engine position apply-draft /tmp/market-price-draft.yaml --db data/app/baibai.sqlite --confirmed
uv run baibai-engine research holding-prepare --db data/app/baibai.sqlite --asof ASOF_DATE --ticker XXXX --workspace .cache/opportunity/ASOF_DATE/holding-XXXX
uv run baibai-engine research thesis-scaffold --workspace .cache/opportunity/ASOF_DATE/holding-XXXX --db data/app/baibai.sqlite --ticker XXXX --sqlite-path data/screening/market.sqlite --target-session NEXT_SESSION_DATE
uv run baibai-engine research review-scaffold --workspace .cache/opportunity/ASOF_DATE/holding-XXXX --db data/app/baibai.sqlite --ticker XXXX
uv run baibai-engine research promote --workspace .cache/opportunity/ASOF_DATE/holding-XXXX --db data/app/baibai.sqlite --ticker XXXX
uv run baibai-engine position holding-review-build --db data/app/baibai.sqlite --thesis-id THESIS_ID --position-id POSITION_ID --out /tmp/holding-review.yaml
uv run baibai-engine position holding-review --db data/app/baibai.sqlite --input /tmp/holding-review.yaml
uv run baibai-engine position holding-review publish /tmp/holding-review.yaml --db data/app/baibai.sqlite --thesis-id THESIS_ID
uv run baibai-engine position sell-result-draft --db data/app/baibai.sqlite --ticker XXXX --quantity 100 --price-yen 1100 --occurred-at YYYY-MM-DDTHH:MM:SS+09:00 --decision-reference HOLDING_REVIEW_ID --out /tmp/sell-draft.yaml
uv run baibai-engine position apply-draft /tmp/sell-draft.yaml --db data/app/baibai.sqlite --confirmed
```

価格下落だけでは売らない。FV到達はreview triggerであり、thesis break、永久損失、current 5年期待値、税・費用控除後の代替価値から判断する。保有数量を超えるsellはbuilderがfail-closedで拒否する。

<a id="annual-outcome-path"></a>

## Annual outcome

portfolio outcomeはcanonical DB ledger、JPX営業日close、配当込みTOPIX observationから再計算し、resolvedになった結果だけをDBへimmutable publishする。期間、source、cash-flow basis不足の`unresolved`結果は不足理由をstdout / ephemeral exportで確認し、正本へ保存せず同じ期間を再実行する。短期結果だけでpolicyを変えない。方法変更は`improvement` sessionから[`improvement-loop.md`](./improvement-loop.md)へhandoffする。

## Completion and stop conditions

session final payloadにはcanonical ID、人間確認結果、1〜3行の`result`と`next`を置く。長いstdout、思考、canonical payload本文、ephemeral HTMLを複製しない。

次では停止して人間へ必要情報を質問する。

- public `--help`とrunbookが一致しない
- coverage、corporate action、価格basis、load-bearing sourceがunresolved
- thesis revision、proposal、reservation、ledger append headがdriftしている
- 人間報告またはrequired fieldがない
- draft作成後にcanonical DBが変わった

完了時は`baibai-engine position ledger --db data/app/baibai.sqlite`と対象entityのqueryを確認し、UIからactive/completed session、proposal current state、portfolio stateが読めることを確認する。
