---
title: "Daily analysis workspace"
summary: "daily machine runからAI判断taskだけを渡し、exact binding・resume・strict result・既存publisherを保つ非正本workspace契約。"
doc_type: reference
status: active
---

# Daily analysis workspace

`baibai-batch analysis`は、既存daily jobと既存domain publisherの間に非正本workspaceを置く。新しいscreening、macro判断、canonical store、売買判断を持たない。役割は、L1入力からdaily machine工程を**産む**、壊れた入力・binding・AI resultを**止める**、token / task / durationを**測る**、operatorへbounded statusとlogを**見せる**ことである。

## 境界

- daily工程の唯一の実装は`baibai_batch.jobs.daily`。`analysis start`は同じjob APIを呼ぶ。
- workspaceとresult cacheは削除可能なlocal stateで、canonical factやjudgmentではない。
- AIはpacket indexと`reused=false`のtask payloadだけを読み、command、path、ID、CAS、publish controlを返さない。
- machineはexact Review Set、run revision、operation、Macro Context、Research Triage headをmanifestへ固定し、既存engine validator / CASで再検証する。
- Research Triage発行後は人間のResearch Set選択を待つ。Macro Contextは自動publishしない。
- 別as-ofのResearch Set選択待ちoperationがあるmanual Macro triggerは、そのoperationを変更せずMacro taskだけを進める。statusは同じ`awaiting_human`でも、`task_types.macro-context`と`macro_phase=independent_complete`がfull-depth第二phase待ちを表し、Research Set選択待ちとは区別する。

既定rootは`${XDG_STATE_HOME:-~/.local/state}/baibai-loop`で、testとschedulerは`--state-dir`で差し替えられる。repository配下へlog、packet、AI resultを作らない。

## CLI

```bash
uv run baibai-batch analysis start \
  --asof YYYY-MM-DD --state-dir <dir> --format json

uv run baibai-batch analysis prepare \
  --daily-manifest <exact-path> --state-dir <dir> --format json

uv run baibai-batch analysis status --workspace <exact-path> --format json
uv run baibai-batch analysis check \
  --workspace <exact-path> --ai-results <exact-path> --format json
uv run baibai-batch analysis publish --workspace <exact-path> --format json
uv run baibai-batch analysis logs \
  --workspace <exact-path> --stage <name> --tail 100
uv run baibai-batch analysis prune-runs \
  --state-dir <dir> --older-than-days <n> --failed-older-than-days <n>
```

`analysis start --verbose`はstep進捗をterminalへmirrorするが、完全出力の正本は同じper-step logである。通常運用では指定しない。
`analysis start --asof`はscheduled local entryとしてmarket calendarの営業日gateを維持する。既存`daily --asof`のmanual rerun契約（gateをskip）は変更しない。

Macro Context monitorは、既存consumerの45日鮮度規則をwarningとして測るが、それ自体を更新義務やAI triggerへ変えない。経済指標の値から新しい機械閾値を作らず、既存方針どおり人間が必要性を判断したmanual triggerだけ`analysis start / prepare --macro-review`で`review`へ上書きする。Research Triage cacheを明示的に無効化する場合は`--re-evaluate`を使う。`--force-new-workspace`はworkspaceを新設するだけで、canonical CASやbindingを迂回しない。

既存`daily`はhuman textをdefaultのまま保ち、`--format json --quiet --manifest-out <path>`で同じ意味結果を1 JSON objectとmanifestへ出せる。

## 状態と再開

```text
started -> machine_complete -> no_ai
                            -> ai_required -> checked -> awaiting_human | published
任意stage -> interrupted | failed
```

lock keyは`daily-analysis + asof`で、競合は`already_running`の正常no-opになる。lock metadataはhost、pid、process start identity、acquired / heartbeat時刻、workspaceを持ち、livenessは経過時間だけでなくkernel lockとprocess identityで区別する。`active.json`がexact run ID、fingerprint、事前配分したpublication identityのdigestを指し、directory scanでlatestを選ばない。resumeはrepo/config/rules/schema fingerprintが一致する場合だけである。workspaceは`run_revision_id`、`review_set_id`、timezone-aware publication clockをdaily開始前に固定する。screening runまたはReview Setが既に発行済みなら、永続step記録とexact IDを照合し、そのwriteより後だけを再開する。mutable inputの再取得からやり直さない。`checked`で中断したrunはmodel/checkを再実行せず既存draftをpublishする。`failed`、特にCAS failureは自動再開せず、CASや「最新」検索で別成果物へ乗り換えない。

未対応workspace schemaは`status`とlog参照だけがread-onlyで可能で、resume / check / publishしない。各create-once writeは`publish-intents/`へtarget ID、source/content digest、expected headを先にfsyncし、同じID・同じ内容のresponse-loss retryだけをexisting successとして照合する。

manifest、pointer、meta、packet、metricsはtemporary fileへのwrite、flush / fsync、atomic replaceで更新する。各step attemptをappendし、再実行で前attemptのlogを上書きしない。directoryは`0700`、fileは`0600`である。state root外へのwriteとsymlink traversalを拒否する。

## Packetとresult

`packet/index.json`はordered task ref、stable `batches[]`、input digest、schema/policy/rules version、bytes、estimated tokenだけを持つ。Research Triageのchanged taskはshared contextとsize budgetが同じ範囲でまとめ、Macro Contextはphase isolationのため別batchにする。task数とmodel invocation数は別metricである。task payloadは対象candidateのmachine snapshot、freshness / unknown / quality flag、同じReview Setへ束縛したMacro Contextのsummary / synthesis / connection projectionへのexact ref、allowlist、length capだけを持つ。raw stdout/stderr、secret、unrelated candidate、canonical path、CAS targetを含めない。

Research Triageのsemantic digestはcandidate snapshot、review order / nomination、temporal validity、Macro binding、rules/policy/schemaを含み、workspace path、run ID、Review Set UUID、generated timeを除く。全digest一致時だけstrict validation済みresultを再利用する。unknown/stale、policy/schema/rules/Macro bindingの変化、`--re-evaluate`では再利用しない。

全Research Triage taskが再利用可能ならmodelを起動せず、machineが空のstrict envelopeとcache済みjudgmentをcomplete setへassembleし、current exact Review Setに対して既存publisherを実行する。task 0件の`no_ai`とは区別し、current canonical Triageを黙って欠落させない。

AI resultはunknown fieldを拒否する。Research Triageで許すのは`verdict`、`rationale`、必要な`research_question`と`key_risk`だけである。task ID / digestの欠落、重複、不一致、length超過、command/path/publish fieldの混入はcanonical write前に失敗する。

## Logとfailure

成功stdoutはstatus、asof、run ID、task / reuse count、workspace、manifest、log directoryだけをbounded表示する。full outputはstep別`stdout.log` / `stderr.log` / `meta.json`へredactして保存し、各logを2 MBでtruncateしてmetaへ記録する。secret、token、password、cookie、Authorizationを共通redactorで除く。

調査順はstatus → manifest reason code → `failure_packet.json`のsanitized hint → bounded tail → 必要時だけexact full logである。成功runのfull logを読まない。required input、corrupt artifact、digest/binding/CAS mismatchだけをjudgment / publish前に止める。Review Setが既に正しくpublishされた後のlog / manifest failureは`observability_degraded`であり、canonical結果をrollbackまたは未publish扱いにしない。

## Verificationとscheduler handoff

`uv run python tools/verification/run_analysis_ops.py --profile fast|full`は、[`python-foundation.md`](./python-foundation.md#9-ci-and-local-parity)の既存commandを固定順で実行し、compact statusとper-command logを出す。別の品質基準は持たない。

timerのinstall / enableはこのrepositoryの責務外である。設定する場合のcommandは1本だけにする。

```text
# Windows Task Scheduler action example
wsl.exe -d Ubuntu --cd /home/<user>/baibai-loop -- \
  uv run baibai-batch analysis start --asof YYYY-MM-DD --format json

# systemd user service ExecStart example
uv run baibai-batch analysis start --asof YYYY-MM-DD --format json
```

ASOFの決定はschedulerまたは呼出元が行い、source本文やmodel outputから生成しない。
