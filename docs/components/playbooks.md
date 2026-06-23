---
title: "Playbooks component"
summary: "Contract for active playbooks stored under records/_playbooks and referenced by research packets."
doc_type: component
status: active
last_reviewed: 2026-05-04
source_paths:
  - "../../records/_playbooks/"
related_docs:
  - "research.md"
  - "../templates/README.md"
---

# Playbooks

`records/_playbooks/` は、research の採用判定で参照する repeatable thesis pattern を保持する運用 asset です。docs ではなく records support area なので、実際に運用で使う本文は `records/_playbooks/` に残します。

## 責務

- research front matter の `playbook` から参照される thesis pattern / evidence checklist を保持する。
- 月次 forward 計測 (`backtest-runbook` の 7 axis) で改訂可否を判断できるよう、versioned Markdown として残す。
- screening 原則、macro context fit、valuation 指標、investment memo 境界を横断する active rule をまとめる。

## 非責務

- docs の一般説明を置かない。
- 過去データに fit したパラメータ探索結果を置かない。
- forward 計測 (lane-cohorts / selection-ablation / screening-replay) 由来の根拠なしに恣意的な閾値変更を行わない。

## Lifecycle

1. 新規 playbook は [`../templates/playbook.md`](../templates/playbook.md) を元に `records/_playbooks/<slug>-v<n>.md` として作る。
2. research は front matter の `playbook` で active playbook を参照する。
3. approved / deferred / rejected decisions は ledger に同期され、`baibai-loop-position benchmark` と `baibai-loop-screening` の forward 計測 (`lane-cohorts` / `selection-ablation`) の input になる。
4. 計測結果 (10 件以上のサンプル) を `reports/<asof>-*.md` にダイジェスト化したうえで、playbook 改訂 issue / PR を起こす。
5. 旧 version は削除せず、research が参照していた当時の rule を追跡できる状態を保つ。

## 現在の playbooks

運用中の一覧は [`../../records/_playbooks/README.md`](../../records/_playbooks/README.md) を正とします。
