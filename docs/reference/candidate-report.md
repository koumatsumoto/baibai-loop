---
title: "Candidate shortlist report"
summary: "opportunity path OP3 の人間レビューgateに出す 8〜10 候補 HTML レポートの生成方式と narratives スキーマ。"
doc_type: reference
status: active
last_reviewed: 2026-07-12
---

# candidate-report — 候補shortlistレポートの生成

`opportunity` path の OP3（[decision-cycle.md](../operations/decision-cycle.md#opportunity-path)）で、人間が深掘り対象を選ぶための 8〜10 候補 HTML レポートを生成する。1 銘柄へ先に決め打ちせず、比較可能な候補群を先に人間へ渡す人間レビューgateの成果物。

## 設計

packet-scaffold と同じく「機械 = data plumbing / 人間 = judgment」。レンダラは screening 出力（`selection-output.yaml` の `audit_pool` と `candidates.yaml` の metrics）から価格・valuation・自己資本比率・net cash・配当 basis・機械 E[r]・FV アンカー乖離・入力 sha256 を機械取得し、**数値を転記しない**。運用者は各候補の定性 narrative だけを `narratives.yaml` に書く。

生成 HTML は `.cache` 配下の **ephemeral 成果物で commit しない**（screen とレンダラの再実行で再現する。records には promote 済み packet/review だけを残す方針と一致）。

## 生成手順

1. OP2 で `candidates.yaml` / `selection-output.yaml`（`--audit-top 20`）を作る。
2. audit pool から 8〜10 候補を選び、[`tools/candidate_report/narratives-template.yaml`](../../tools/candidate_report/narratives-template.yaml) を run の workspace へ複製して記入する。
3. レンダラを実行する。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m tools.candidate_report.render \
  --selection .cache/opportunity/YYYY-MM-DD/selection-output.yaml \
  --candidates .cache/opportunity/YYYY-MM-DD/candidates.yaml \
  --narratives .cache/opportunity/YYYY-MM-DD/narratives.yaml \
  --out .cache/opportunity/YYYY-MM-DD/candidate-report.html
```

## narratives スキーマ

- `meta`: `title` / `target_session` / `order_by`（表示順の説明。推奨順位ではない）/ `intro_notes`（brief 直下の箇条書き。screening 修正や basis の注記）。
- `candidates`: 順序 = 表示順。各 `ticker` は当該 run の `audit_pool` に含まれること。`ploss` は `低 / 中低 / 中 / 要精査 / 高`。定性 key は `why`（なぜ安い）/ `temporary`（一時的か）/ `structural`（構造的か）/ `survive`（5年耐性）/ `unlock`（株主価値向上要因）/ `counter`（最強反対仮説）/ `research`（個別リサーチ確認事項）/ `value`（深掘り価値）/ `prov`（暫定判断）。`sector_label` は任意（未指定なら screen の `sector_33`）。
- `excluded`: audit pool で非選択の ticker と具体的理由。「順位が低い」「予算外」だけは不可。投資対象外もここへ理由付きで。

audit pool に無い ticker を narrative に書くとレンダラは error で停止する（存在しない数値を出さない）。

## Related

- [`../operations/decision-cycle.md`](../operations/decision-cycle.md)
- [`../workflow/research.md`](../workflow/research.md)
