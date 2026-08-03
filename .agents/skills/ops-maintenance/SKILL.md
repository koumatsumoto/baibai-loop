---
name: ops-maintenance
description: 機械の健全性維持。daily batch 監視、store 同期（R2 push/pull）、materialize、障害対応、calibration panel と PMI manifest の月次維持、運用 task の規約。
---

# Ops Maintenance

投資判断はしない。事実の生成・検証・配信の再現性を守る。

## Daily batch 監視

`cloud-daily-batch` が東証営業日 18:30 JST に coverage → EDINET 抽出 → run → select → macro refresh → export → prune を 1 コマンドで回す（契約は [`tools/cloud/README.md`](../../../tools/cloud/README.md)）。Discord `#batch-runs` の `[OK]` / 失敗通知が唯一の push 経路で、通知本文に当日の差分件数が載る。

失敗時の入口:

- **source 取得失敗**: Baibai App Macro タブ上部の要約カード（取得失敗・stale 件数）→ 該当行。系列別の成否は `provider_runs`。取得失敗を「未公表」と混同しない。Tier 1 が取れないときは [`data-sources.md`](../../../docs/reference/data-sources.md) の Tier 2 例外運用。
- **validation 失敗**: application service / DB constraint / model validation の error path を読み、schema・validator の意味を推測で変えない（必要なら issue）。
- **automation 失敗**: screening CLI は [`screening-runtime.md`](../../../docs/reference/screening-runtime.md)、バッチ経路は [`architecture.md#cloud-serving-layer`](../../../docs/architecture.md#cloud-serving-layer)、CI/local parity は [`python-foundation.md`](../../../docs/reference/python-foundation.md)。

失敗 run は publish が skip され正本は変わらない。復旧後の再実行は `gh workflow run cloud-daily-batch`（必要なら `MANUAL_ASOF` dispatch input）。**古い workflow revision の rerun は使わない**（main の現行コードで dispatch し直す）。

## Store 同期（`tools/cloud/r2_transfer.sh`）

| store | 正本 | 転送規律 |
| --- | --- | --- |
| market / machine（runs） | R2 | 読む前に `pull-market` / `pull-machine`。push は script が **R2 copy を merge してから upload**（merge-then-push）。ローカルだけで長く作業した store を直接 push しない |
| macro（indicators） | R2 | 同上（`push-macro` は no-loss merge。誤値の訂正は削除でなく `macro retract` — 契約は [`macro.md`](../../../docs/reference/macro.md)） |
| app（baibai.sqlite） | **local** | 判断はローカルが正本。publish 後に `push-app`（直 push）→ materialize。**pull しない** — `pull-app` はローカルに store があれば止まる（cloud copy で置換すると未 push の判断が消える）|

cloud 障害は「store が code より古い」形で出ることが多い。再現はローカルへ R2 store を pull して read 経路を通す。

## Materialize（serving 反映）

app / macro を publish したら `gh workflow run cloud-materialize` を dispatch し、run の completed success を確認する。view shape を変える deploy では **code deploy → materialize の順**を守り、UI は旧 view で graceful degrade できることを確認する。

## 月次維持

- **calibration panel**: `uv run baibai-engine screening calibration-build --start 2022-09-01 --end <直近の完全月末>`（増分。rules 改訂後は `--force` 再構築）→ `calibration-evaluate`。契約は [`estimate-calibration.md`](../../../docs/reference/estimate-calibration.md)。
- **PMI manifest**: `uv run python tools/append_pmi_manifest.py --dry-run` → 本実行 → `macro refresh` で該当月を取得し公表値と照合してから commit。月が飛ぶ追記は拒否される（先に穴を埋める）。

## 運用 task の規約

運用 task の正本は application DB（`baibai-engine task` が唯一の writer）。GitHub Issue は開発作業（feature / bug / 基盤改善）に使い、task 管理には使わない。

- **対象**: defer thesis の再確認 event、決算後の holding review、実行日付きの判断待ち、注文期限後の運用確認。日付のない改善案は対象外（issue へ）。
- **粒度**: 同じ期限・種別・領域の未保有候補は 1 件に統合。保有・売買判断・高重要は個別。追加前に `task list --status open` で既存と照合する。
- **body**: load-bearing question / primary sources / expected destination / close condition だけを短く。作業ログを複製しない。
- **状態**: `done`（判断と canonical 更新完了）/ `dropped`（問いが不成立）。判断が変わった理由は thesis 等の正本へ書く。
- **resume**: `uv run baibai-engine task list --status open` を due 順に読み、current canonical entity・ledger と照合して trigger 到来の task を 1 件選ぶ。矛盾したら停止し、推定で終端へ進めない。
