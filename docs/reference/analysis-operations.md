---
title: "Daily analysis run"
summary: "cloud正本のReview Setを1回のbounded AI判断へ渡し、canonical validationを保って発行する契約。"
doc_type: reference
status: active
---

# Daily analysis run

`baibai-batch analysis run`はpull済みのcanonical runs storeから対象日のReview Setを読み、Research Triage publishまでを1 commandで実行する。Screening RunとReview Setはcloud dailyだけが生成する。local analysisはscreening、Review Set publish、macro series更新、read model export、prune、task reconcileを行わない。full-depth Macro Contextはmanualの`macro-context` skillだけが扱う。

```bash
uv run baibai-batch analysis run
uv run baibai-batch analysis run --asof YYYY-MM-DD  # 手動再実行
```

## 実行境界

1. `flock`でlocal analysisを1本に制限し、JST基準日を決める。
2. `--asof`未指定時だけmarket calendarでJST当日の営業日を判定する。指定時はその日をexactに対象とする。
3. runs storeから対象`as_of`で`created_at`が最新のcanonical Review Setを1件読み、AI不要条件を確定する。対象日に無ければ前営業日へfallbackしない。
4. Review Set全体、短い[`TRIAGE_POLICY`](../../batch/src/baibai_batch/analysis/policy.py)、利用可能なMacro Contextの共有projectionを1つのstdin payloadにする。
5. local `codex exec`を原則1 process・1 requestで実行し、strict JSONだけを受け取る。
6. ticker集合、重複、欠落、field shape、長さを検証する。
7. AI入力へ載せたMacro Context IDを保持したまま`baibai_engine.batch_api`経由で既存`ResearchTriageService`へ委譲し、binding、candidate snapshot、head CAS、same-ID idempotencyを再検証してpublishする。
8. publish結果を返して終了する。Research Setの確定とOperation開始は`research prepare --ticker`の人間gateが所有する。

AIはfilesystem path、command、run / Review Set ID、CAS、digest、publish操作を受け取らない。repository、skill、runbook、CLI help、raw logを読まず、出力は`ticker / verdict / priority / rationale / research_question / key_risk`に限定する。AIは1 requestで全候補を比較し、`research`だけへ1..Nのcontiguous priorityを付ける。Review Setのticker serialization順やE[r]順をpriorityとして複写しない。

## Model process 0

次はAI起動前に終了する。

- 非営業日
- Review Setなし、または0件
- exact Review Setのcanonical Research Triageが既にある
- Review Set、candidate snapshot、application store等の必須machine inputが欠損・破損している

Review Setなしは`no_review_set`、既存Triageがあれば`awaiting_human`または`already_published`を返す。active Operationの有無はTriageの生成条件に含めない。別Operationがactiveでもdaily Triageは発行でき、新しいResearch開始だけを`research prepare`が拒否する。

## Failureと再実行

AI result不正、adapter failure、binding / CAS conflictでは新しいResearch Triageを書かない。次回は同じ対象日のcanonical Review Setを使って`analysis run`をfreshに実行する。canonical Triageが既に存在する場合だけexact Review Setで照合し、modelとpublishを重複実行しない。active pointer、heartbeat、lease、publish intent、stage resume、candidate cacheは持たない。

## 出力とlocal artifact

stdoutはstatus、as-of、model process / request数、input bytes、actual token（取得できる場合）、research / skip数、human action、log pathだけをcompactに出す。`summary.json`にはmachine command 0とAI duration、tool / file read数を残す。非営業日、Review Setなし、空Review Set、exact既存Triageはexit 0とし、calendar、store、AI result、binding、CAS、canonical writeのfailureはnon-zeroを維持する。通常成功時にlogを読む必要はない。

各runは`${XDG_STATE_HOME:-~/.local/state}/baibai-loop/analysis/<timestamp>/`に最大4 artifactを置く。

- `input.json`: modelへ渡す1つのsanitized payload
- `result.json`: strict model result
- `summary.json`: model / token / duration / final status
- `run.log`: private、redacted、2 MB上限の詳細log

非営業日等のmodel 0経路では不要な`input.json`と`result.json`を作らない。directoryは`0700`、fileは`0600`とし、credentialを含み得るstdout / stderrはredactしてlogだけへ置く。adapterにはCodex認証と標準runtimeに必要な環境変数だけを渡し、cloud / market provider credentialを継承しない。
