---
title: "Trade 成績向上アイデア — 5 並列調査の統合"
summary: "5 専門 agent (trade record 定量分析 / screening rules / selection layer / 未活用 signal / workflow) で深堀りした結果を統合し、優先度マトリクスにまとめる。実装ではなく仮説 / アイデア出しの集約。"
doc_type: report
status: active
date: 2026-06-19
related_docs:
  - "./2026-06-17-trade-strategy-rootcause.md"
  - "../docs/operations/backtest-runbook.md"
  - "../docs/screening/mechanical.md"
---

# Trade 成績向上アイデア (5 並列調査統合)

## 0. 動機 (現状診断)

8 件 open trade、abs +2.00% / benchmark +3.29% = **rel −1.29pt**, rel 勝率 37.5%、rel PF 0.38。bootstrap CI [−7.60, +0.32]pt, P(true<0)=96.2% (n=8, eval=6/12)。承認 → trade 転換率 100% で entry friction ゼロ、approved 8/8 全件が trade に直結。

## 1. 最重要発見 (1 行ずつ)

| # | 発見 | source | impact |
|---|---|---|---|
| F1 | **lane priority order が forward return と矛盾**: config が valuation-reversion を first に置くが、backtest で worst (-11.5pt)、cash-rich が best (+5.32pt baseline 比) | A2 | 高 |
| F2 | **selection_ablation で market_regime が渡されていない bug**: fast_boost Δfull = +10.1pt は misleading、production 5/6 週で regime gate 中立化済み | A3 | 高 (計測 infra) |
| F3 | **max_recommended_per_sector=1 が良候補を取り逃がしている**: `no_diversity` ablation で **Δfull = +1.9pt (4w)** | A3 | 高 |
| F4 | **back-fill 群 3 件 (9682/9692/9470) が cohort を歪めている**: cum rel -12.62pt vs 規律下 5 件 +0.65pt。混合評価が現行 workflow を不当に過小評価 | A1 | 高 (計測 infra) |
| F5 | **per_forward 計算済みだが lane gate で未使用**: docs/screening/valuation-metrics.md は「forward 優先」と mandate するが、valuation-reversion.metrics は [per_trailing, pbr, ev_ebitda] のみ | A2 | 高 |
| F6 | **情報・通信業 64.1% 集中**: sector cap 45% を超え warning だが proceed、rally で IT lag 被弾 | A1 | 高 |
| F7 | **EDINET ttm metrics 大量未使用**: `operating_profit_ttm` / `fcf_ttm` / `capex_ttm` / `total_assets` / `equity` (audited) が schema 入りだが gate / rank で活用ゼロ | A2/A4 | 中 |
| F8 | **OHLV (open/high/low/volume) が provider layer で drop**: SQLite には保存済みだが selection ranking で読まれない → realized vol / ATR / Parkinson / gap 未活用 | A4 | 中 |
| F9 | **closed trade の exit attribution forward 計測なし**: stop / target / time / catalyst exit がどう効いたか測れない → 新 exit lane の価値検証不可 (philosophy 柱 5 違反) | A5 | 中 |
| F10 | **dead code 多数**: no_prior_suppression (Δfull +0.0pt), long_hold_survivability (high 比率 85-90% で discriminator 死), profile 抽象 (1 値固定 dead code) | A3 | 低 (清掃) |

## 2. 優先度マトリクス

評価軸: **期待効果 (forward return への寄与見込み)** × **実装コスト** × **計測経路の確実性** (philosophy 柱 5)。

### A. 即実装・即効果 (config 変更のみ、ablation 確認済)

| ID | 内容 | 期待効果 | 実装コスト | 計測 | 根拠 |
|---|---|---|---|---|---|
| A1 | **lane order を `[cash-rich, valuation, cashflow, sales]` に並び替え** | mean rel 推定 +1〜2pt (lane-cohorts 既測) | yaml 1 行 | replay diff | A2 F1, lane-cohorts 2026-05 |
| A2 | **`max_recommended_per_sector` を 1 → 2 に緩和** | mean rel +1.9pt (4w ablation) | yaml 1 行 | ablation 既測 | A3 F3 |
| A3 | **valuation-reversion gate に `per_forward` 追加 (degrade to per_trailing if null)** | rally 局面の銘柄選別精度向上 | rule_config + rules.py ~5 行 | replay diff | A2 F5, docs/valuation-metrics.md mandate |
| A4 | **金融 + 電気・ガス + 卸売業 + 不動産業を `cash-rich-asset-discount` から除外** | cash_eq 見かけ膨張による偽 hit を排除 | rules.py ~3 行 | lane-cohorts 再計測 | A2「卸売 49/292 cash-rich hit, 商社 working capital で cash_eq 膨張」 |

### B. 計測 infra 修正 (即着手、新 feature 評価の base)

| ID | 内容 | 期待効果 | 実装コスト | 計測 | 根拠 |
|---|---|---|---|---|---|
| B1 | **selection_ablation に `market_regime` を渡す bug 修正** | fast_boost Δfull の真値を測れるようになる、現在 +10.1pt は誤計測 | screening_replay.py ~3 行 | ablation 再走 | A3 F2 |
| B2 | **back-fill cohort 分離**: trade record の `pre_refactor_history_restored` / `user_position_confirmed_after_screening` reason_code を ledger CLI で除外フラグ化 | "現行 workflow 純度" を計測可能に → rel -1.17pt の解像度を「規律下 +0.65pt / 規律外 -4.21pt」に分解 | ledger/benchmark.py 拡張 ~20 行 | benchmark CLI に flag | A1 F4 |
| B3 | **closed trade の exit attribution forward 計測**: `executions[]` + `planned_exit` の差分から「どの exit lane (stop / target / time / catalyst) が realized return を生んだか」集計 | 新 exit lane (trailing / partial / catalyst) の forward 計測経路を確立 | ledger CLI 新 sub-command ~80 行 | output に attribution dict | A5 F9 |

### C. screening rule 強化 (中コスト、各々 lane-cohorts で価値検証)

| ID | 内容 | 期待効果 | 実装コスト | 計測 | 根拠 |
|---|---|---|---|---|---|
| C1 | **`_has_deterioration` を cashflow-yield / cash-rich gate にも拡張** | -50% EPS 会社が cashflow-yield 経由で通る穴を塞ぐ | rules.py ~10 行 | drop_rule replay | A2 F「OR-passing で _has_deterioration が valuation-reversion のみ block」 |
| C2 | **`_sales_operating_profit_gate` に operating margin floor (e.g. > -0.05) 追加** | chronic loser (-100B → -50B "loss narrowing") を排除 | rules.py ~5 行 | lane-cohorts | A2 F「-9.7pt sales-discount-growth cohort を構造的に説明」 |
| C3 | **EDINET `operating_profit_ttm` を J-Quants single-period に優先**: cash-rich / sales-discount gate で TTM 採用 | 季節 Q noise の影響を低減 | metrics.py ~10 行 | replay diff | A2 F7 |
| C4 | **cashflow-yield-discount に FCF-positive gate 追加**: ocf+/fcf- の重設備銘柄を排除 | 設備投資先行で free cash 出ない銘柄を除外 | rules.py ~5 行 | drop_rule replay | A2 提案 (e) |
| C5 | **`sector_median_gap_max` / `sigma_gap_max` / `cfo_yoy_min` の sensitivity grid** | threshold の magic number を計測ベースに | replay grid script | grid search | A2「pre-register −0.5/−1.5 grid replays」 |

### D. 新規 signal (academic factor、既存 data から計算可)

| ID | 内容 | 期待効果 | 実装コスト | 計測 | 根拠 |
|---|---|---|---|---|---|
| D1 | **realized vol (60d, Parkinson) lens**: 既存 OHLV から計算、低 vol / 高 vol を ranking 重み or 除外条件に | low-vol effect は JP small/mid で生き残る anomaly (Ang-Hodrick-Xing-Zhang) | metrics.py + lenses.py ~50 行 | ablation 新 variant | A4 academic factor #3 |
| D2 | **accruals = (NI − CFO) / TA**: 全 input 既存。high accruals は forward EPS 下方修正の予兆 (Sloan 1996) | quality-cheap tilt で earnings-quality trap 排除 | metrics.py ~20 行 | lane evidence_hit 追加 | A4 academic factor #1 |
| D3 | **shares_outstanding 差分 (net issuance / buyback)**: 既 fetch、period 間 diff | 自社株買い銘柄を bonus、増資銘柄を penalty | metrics.py ~15 行 | lane evidence | A4 (B) |
| D4 | **upper/lower limit flags の集計 (week 内 stop 回数)**: 既 fetch、未読 | センチメント spike / panic 検出 | metrics.py ~10 行 | descriptive 先行 | A4 (A) |
| D5 | **業種別 momentum + 1-month reversal**: 既存 close で計算可 | sector rotation catch (短期 reversal 補強) | metrics.py ~20 行 | lane evidence | A4 academic factor #4 |
| D6 | **PEAD (post-earnings drift)**: `forecast_eps` vs `eps_ttm` を surprise proxy、1-12w drift | JP で robust な earnings surprise anomaly | metrics.py ~30 行 + earnings calendar 拡張 | catalyst lane | A4 academic factor #5 |

### E. workflow / sizing / exit (中-高コスト、structural 改善)

| ID | 内容 | 期待効果 | 実装コスト | 計測 | 根拠 |
|---|---|---|---|---|---|
| E1 | **research front matter に `screening_context: {rank, regime_lens_score, short_signal_evidence_hits, next_earnings_date, sector_concentration_warning}` 追加** | discretionary override を構造化、9534 のような override 群 vs 規律群 forward 比較 | schema + validator ~30 行 | validator finding | A5 F「9534 case」 |
| E2 | **`planned_exit.event_reviews[]` field 化**: catalyst-based exit を systematic に | invalidation_conditions が trigger date 越えたら outcome 必須 (validator enforce) | schema + validator ~40 行 | validator + ledger | A5「3539 Q3 manual log のみ」 |
| E3 | **sector concentration を warning → hard cap (validator block)** に格上げ | 情報・通信業 64.1% のような単一 sector 集中を構造的に防ぐ | policy_config + validator ~10 行 | trade validator | A1 F6 |
| E4 | **time_stop を lane 別に分ける**: cashflow-yield-discount = long-hold 寄り、valuation-reversion = 短期 mean-reversion で異なる horizon | exit timing の lane 適合性向上 | trade schema + validator ~20 行 | exit attribution (B3 前提) | A5 D「time_stop = 40 営業日固定」 |
| E5 | **macro_fit `neutral` を「safe default」扱いしない preflight 追加**: neutral + rally + no_catalyst → "starter size 限定" を強制 | A1 F「macro_fit neutral n=3 mean -4.21pt」最悪パターンを構造化 | validate/research/core.py ~15 行 | research validator finding | A1 F |
| E6 | **conviction-driven sizing**: `risk_reward_ratio` から擬似 Kelly fraction で real sizing scaling | starter (100株固定) RR=2.0 と RR=3.5 で同サイズの歪み解消 | policy + validator ~25 行 | benchmark cohort | A5 B |

### F. dead code 整理 (低コスト、保守性)

| ID | 内容 | 削減 | 根拠 |
|---|---|---|---|
| F1 | `no_prior_suppression` ablation variant 削除 (Δfull +0.0pt no-op) | ablation noise 削減 | A3 F |
| F2 | `long_hold_survivability` を ranking から外す or annotation 簡素化 (high 比率 85-90% で discriminator 死) | output 軽量化 | A3 F |
| F3 | `no_stabilization` variant 削除 (fast_boost 非活性で恒等 1) | ablation noise 削減 | A3 F |
| F4 | profile 抽象 (`BUILTIN_SELECTION_PROFILES={"balanced"}` 1 値固定) を削除、selection_rules 直接呼ぶ | dead code 整理 | A3 F |
| F5 | 2026-06-12 candidates regenerate して stale `strict-net-cash` / `fcf-yield` evidence_hits 削除 | config drift 解消 | A2 提案 (i) |

## 3. recommended sequencing

> philosophy 柱 5 「計測経路のない機能は追加しない」を守るため、**計測 infra 整備 (B) を最優先**にする。次に **config 変更で即効果 (A)**、その後 **新 signal (D) と structural 改善 (C/E)** を ablation で価値検証しながら積む。dead code 整理 (F) は副次。

**Phase 1 (1 PR, 1-2日)**: 計測 infra 整備 + config 即効果
- B1 (ablation bug 修正)、A1 (lane order)、A2 (diversity cap 緩和)、A3 (per_forward gate)
- 各々 replay diff で no-regression / 改善幅を 6 週 multi-period で計測
- ablation variant の真値が分かったうえで次 phase に進む

**Phase 2 (1 PR, 2-3日)**: back-fill 分離 + screening rule 強化
- B2 (back-fill cohort 分離)、A4 (sector 除外修正)、C1-C4 (rule 穴塞ぎ)
- 既存 8 trade の forward attribution を「規律下 cohort」のみで再評価
- C5 (sensitivity grid) はオプション

**Phase 3 (1 PR, 3-5日)**: 新規 signal evidence_hit
- D1 (realized vol)、D2 (accruals)、D3 (shares delta) — 既存 data で完結
- 各 signal を新 lane の evidence_hit に追加、lane-cohorts で価値検証
- D4-D6 は次々回 PR

**Phase 4 (1 PR, 3-5日)**: workflow / exit infra
- B3 (exit attribution forward 計測) → これで E2/E4 が forward 検証可能に
- E1 (screening_context structured)、E3 (sector hard cap)、E5 (macro_fit neutral preflight)

**Phase 5 (1 PR, 2-3日)**: dead code 整理 (F1-F5) + docs

## 4. リスクと limitation

- **小サンプル**: n=8 trade、forward 1-31bd の不均一、single regime (risk_on_rally) のみ → cohort 統計はノイズ多。bootstrap CI を必ず併記
- **lane-cohorts は overlap 量に bias**: cash-rich を first にすると他 lane の primary evidence ピックも変わる → drop_lane と reorder を別計測
- **新 signal の overfitting risk**: academic factor は in-sample で生き残るが local market では崩れることあり → hold-out 計測必須 (backtest-runbook §4)
- **back-fill cohort 分離は判断 risk**: 規律外 trade を除外して評価精度が上がる一方、「実際の P&L」は混合のまま → 両方を併記

## 5. 候補から除外 (採用見送り or 後回し)

- **paper_proxy 抽象削除**: PR #249 で defer 確定、forward 計測経路 (ADV 5% cap, paper_to_real 換算) あり
- **freshness.py 削除**: PR #249 で defer 確定、sizing_eligible gate forward 経路あり
- **TDnet adapter (A4 D1)**: 価値高だが adapter 実装コスト大 → Phase 5 以降
- **J-Quants Standard/Premium upgrade**: 月額発生、現状 Light で D1-D5 を試して効果見てから判断
- **process pool 並列化**: PR #250 で defer 確定 (-4.5% only、保守性 trade-off)

## 6. 各 agent 詳細 source

- A1: `/tmp/claude-1000/.../tasks/a9dc8ff38618a8c55.output` (trade record 定量分析)
- A2: `/tmp/claude-1000/.../tasks/abf0615db61264a71.output` (screening rules / lane)
- A3: `/tmp/claude-1000/.../tasks/a4b2a50b32a2e9e5a.output` (selection layer)
- A4: `/tmp/claude-1000/.../tasks/a76dc47c2cf54c7d5.output` (未活用 signal)
- A5: `/tmp/claude-1000/.../tasks/a96721dc7bc64bc40.output` (workflow / sizing / exit)
