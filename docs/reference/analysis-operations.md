---
title: "Research Triage runner"
summary: "Triage runnerの入力境界、終了状態、結果の所在。"
doc_type: reference
status: active
---

# Research Triage runner

`baibai-batch analysis run`は、保存済みの対象日Review Setを一つのAI判断へ渡し、engineのserviceを通してTriageを発行する。Screening Run・Review Setの生成、macro取得、cloud公開は行わない。操作の順序は[Research Triage skill](../../.agents/skills/research-triage/SKILL.md)が所有する。

## 入力と判断の境界

対象as-ofの最新Review Setを使い、別日へfallbackしない。Review Set全件、[TRIAGE_POLICY](../../batch/src/baibai_batch/analysis/policy.py)、利用可能なMacro Contextの共有projectionを一つのpayloadとして渡す。AIにrepository・runbook・成功logを読ませず、ID・CAS・publish操作を判断させない。

Triageの対象集合と保存bindingはengineが検証する。候補を`research / skip`へ分類し、調査優先度を決めるもので、購入やOperation開始ではない。active OperationがあってもTriageは実行できる。

## 終了状態

| status | 意味 | model起動 |
| --- | --- | --- |
| `already_running` | local analysisのlockを取得できない | 0 |
| `no_review_set` | 対象日のReview Setがない | 0 |
| `empty_review_set` | 対象Review Setのentryが0件 | 0 |
| `awaiting_human` | exact Review SetのTriageが既存で、research候補がある | 0 |
| `already_published` | exact Review SetのTriageが既存で、全skip | 0 |
| `published_awaiting_human` | 今回の発行経路が成功し、research候補がある | 通常1 |
| `published_all_skip` | 今回の発行経路が成功し、全skip | 通常1 |
| `failed` | 入力・AI・binding・write等の失敗 | 発生地点による |

正常状態はexit 0、失敗はnon-zeroである。非営業日であること自体はno-op条件ではない。正常な対象なしと、保存物の破損を区別する。

## 結果と再実行

成功時はcanonical Triage ID、as-of、priority順の全research候補を返す。人間が選ぶ対象はこのexact IDへ結び付ける。

実行結果と必要な診断は`${XDG_STATE_HOME:-~/.local/state}/baibai-loop/analysis/<timestamp>/`の`summary.json`、`run.log`等にある。通常成功時のlog再読は不要である。機密を含み得るadapter出力はredactionされたprivate logとして扱う。

表示やlog保存が失敗しても、canonical writeの有無を終了コードだけで決めない。原因を解消して同じ対象日をfreshに実行すると、既存のexact Review Set照合がTriageの再生成を避ける。旧workspaceのresumeや、別日のlatestによる補完はしない。
