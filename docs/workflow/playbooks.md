---
title: "Workflow — playbooks"
summary: "割安 value の型（archetype）：research が参照する再現可能な thesis の型。records/_playbooks/ に版管理した Markdown で保持し、見積りの較正結果に基づいて改訂する。"
doc_type: workflow
status: active
last_reviewed: 2026-07-02
---

# Workflow — 戦略プレイブック（value archetype）

`records/_playbooks/` は、[`./research.md`](./research.md) の採用判定で参照する **再現可能な割安 value の型（archetype）** を保持する運用資産。docs ではなく records の支援領域なので、運用で使う本文は `records/_playbooks/` に置く。判定条件は [`./screening.md`](./screening.md) の playbook 連動 screen と対応する。

## 責務 / 非責務

**責務**：research の front matter `playbook` から参照される thesis の型と根拠チェックリストを保持する。screening の判定・valuation 指標・塩漬け耐性・投資メモの境界にまたがる現行ルールをまとめる。版管理した Markdown で残し、見積りの較正結果（[`../doctrine.md`](../doctrine.md) 柱 3）から改訂の要否を判断できる状態を保つ。

**非責務**：docs にあるべき一般的な説明は置かない。過去データに適合させたパラメータ探索の結果は置かない。較正の根拠なしに閾値を恣意的に変更しない。

## Value archetype

playbook は「どのタイプの割安を、どの耐性条件で買い、どの条件で holding review を起こすか」を定めた型。長期積立に合う少数の型に整理する。

- **割安 value（現金 / キャッシュフロー / 資産系）**：ネットキャッシュ・営業キャッシュフロー・資産価値に対する割安を、業種相対 / 自己レンジ相対の percentile と個別のフェアバリューで拾う。構造的に塩漬け耐性が厚い（[`./screening.md`](./screening.md) の `cash-rich-asset-discount` / `cashflow-yield-discount` / `valuation-reversion` が対応）。
- **配当インカム**：安定した配当・株主還元のある割安銘柄を、資金が拘束されている間の収益源として拾う。減配や還元方針の毀損を無効化条件（invalidation）にする。

各型の本体は次を含む：対象範囲・狙い・判定条件（valuation / 財務）・塩漬け耐性の確認項目・**FV 到達時の review trigger と thesis break の条件**・無効化条件・永久損失軸の確認。FV 到達は自動売却ではなく、`hold / add / reduce / exit` の正本は [`../reference/holding-review.md`](../reference/holding-review.md) とする。保有期間は固定しない。

## Lifecycle

1. 新規 archetype は `records/_playbooks/<slug>/` に versioned Markdown で作る。
2. research は front matter の `playbook` で active archetype を参照する。
3. 保有の実現結果（実際のリターン・利回り・valuation の収束・thesis の的中）を [`./position.md`](./position.md) の `estimate_calibration` に蓄積する。
4. 較正の結果が「この型の見積りは系統的に外れている / 有効に機能している」を示したら、型を改訂する issue / PR を起こす。判定条件を変える場合は [`./screening.md`](./screening.md) の screen ルール（`records/_config/screening-rules/*.yaml`）と揃える。
5. 旧版は削除せず、research が参照していた当時のルールを後から追跡できる状態を保つ。

## 現在の playbooks

運用中の一覧は [`../../records/_playbooks/README.md`](../../records/_playbooks/README.md) を正とする。

## 参考

- [`./research.md`](./research.md)：playbook を参照する採用判定
- [`./screening.md`](./screening.md)：archetype に対応する機械 screen
- [`../portfolio-management.md`](../portfolio-management.md)：cap・耐性ゲート
- [`../doctrine.md`](../doctrine.md)：柱 3（見積りの較正に基づいて改訂）
