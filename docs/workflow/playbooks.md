---
title: "Workflow — playbooks"
summary: "割安 value の archetype：research が参照する再現可能な thesis pattern。records/_playbooks/ に versioned Markdown で保持し、見積り calibration で改訂する。"
doc_type: workflow
status: active
last_reviewed: 2026-07-01
---

# Workflow — 戦略プレイブック（value archetype）

`records/_playbooks/` は、[`./research.md`](./research.md) の採用判定で参照する **再現可能な割安 value の thesis pattern（archetype）** を保持する運用 asset。docs ではなく records support area なので、運用で使う本文は `records/_playbooks/` に残す。判定条件は [`./screening.md`](./screening.md) の playbook-linked screen と対応する。

## 責務 / 非責務

**責務**：research front matter の `playbook` から参照される thesis pattern と evidence checklist を保持する。screening 判定・valuation 指標・塩漬け耐性・investment memo 境界を横断する active rule をまとめる。versioned Markdown で残し、見積り calibration（[`../doctrine.md`](../doctrine.md) 柱 3）で改訂可否を判断できる状態にする。

**非責務**：docs の一般説明を置かない。過去データに fit したパラメータ探索結果を置かない。calibration の根拠なしに恣意的な閾値変更をしない。

## Value archetype

playbook は「どの割安タイプを、どの耐性で、いつ全売りするか」の型。長期積立に合う少数の archetype に整理する。

- **割安 value（cash / CF / asset 系）**：net-cash・営業 CF・資産価値の割安を、業種相対 / 自己レンジ相対の percentile と個別 FV で拾う。塩漬け耐性が構造的に厚い（[`./screening.md`](./screening.md) の `cash-rich-asset-discount` / `cashflow-yield-discount` / `valuation-reversion` が対応）。
- **配当インカム**：安定した配当・還元がある割安銘柄を、資産ロック中の収益源として拾う。減配・還元方針の毀損を invalidation にする。

各 archetype の本体は次を含む：対象 universe・狙い・判定条件（valuation / 財務）・塩漬け耐性の確認項目・**全売りの条件（割高化＝FV 到達 or 割高ゾーン、および fundamental 毀損）**・invalidation・kill switch 確認。保有期間は固定しない（期間ではなく valuation と耐性で判断する）。

## Lifecycle

1. 新規 archetype は `records/_playbooks/<slug>/` に versioned Markdown で作る。
2. research は front matter の `playbook` で active archetype を参照する。
3. 保有の実現結果（realized return / yield・valuation 収束・thesis 的中）を [`./position.md`](./position.md) の `estimate_calibration` に蓄積する。
4. calibration が「この archetype の見積りが系統的に外れる / 有効」を示したら、archetype 改訂の issue / PR を起こす。判定条件を変える場合は [`./screening.md`](./screening.md) の screen rule（`records/_config/screening-rules/*.yaml`）と揃える。
5. 旧 version は削除せず、research が参照していた当時の rule を追跡できる状態を保つ。

## 現在の playbooks

運用中の一覧は [`../../records/_playbooks/README.md`](../../records/_playbooks/README.md) を正とする。

## 参考

- [`./research.md`](./research.md)：playbook を参照する採用判定
- [`./screening.md`](./screening.md)：archetype に対応する機械 screen
- [`../portfolio-management.md`](../portfolio-management.md)：cap・耐性ゲート
- [`../doctrine.md`](../doctrine.md)：柱 3（見積り calibration で改訂）
