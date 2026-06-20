---
title: "Backtest runbook"
summary: "Multi-axis backtest 手順。screening / judgment gate / lane / regime の forward return を、look-ahead を排除した形で再現可能に計測する。"
doc_type: runbook
status: active
last_reviewed: 2026-06-17
related_docs:
  - "../screening/mechanical.md"
  - "../components/research.md"
  - "../../reports/2026-06-17-trade-strategy-rootcause.md"
---

# Backtest runbook

戦略 / screening / judgment gate / playbook lane の forward return を **「いつ、どう測るか」** を統一する。本 doc は**手順と原則**を正本にし、dated 計測ダイジェストは `reports/<YYYY-MM-DD>-*.md` に残す。

## 1. 何を測れば「意味のあるバックテスト」か

`docs/philosophy.md` 柱 5（計測ファースト）と [`../design-principles.md`](../design-principles.md) §9（やる backtest と避ける最適化の線引き）に従い、以下を全て満たす:

1. **forward 計測**: 判断時点（asof）に存在した情報のみ使い、未来情報を予測材料に使わない (look-ahead bias 排除)
2. **same-basis 価格**: stock / benchmark を同じ価格基準（adjusted close、resolve-on-or-before）で揃える
3. **horizon を揃える**: 比較する 2 群を同じ entry → 同じ eval cap で見る（+15bd 固定など eval 日が異なるものは bias）
4. **sample-size honest disclosure**: n が小さい bucket は明示し、bootstrap CI 等で uncertainty を可視化
5. **counterfactual の明示**: 「もし規律 X を適用していたら何が変わったか」を再構成可能にする
6. **multi-axis**: 単一 axis では確証 / 反証が出ないため、最低 4 axis（regime / lane / outcome / week）で交差させる
7. **out-of-sample 分離**: hypothesis-generating データと validation データを分け、in-sample fit の楽観を排除

## 2. データの正本

| データ | 場所 | 価格基準 |
| --- | --- | --- |
| 全上場日足 | `data/screening/market.sqlite` `jquants_daily_bars` | `adjustment_close` 優先、無ければ `close` |
| benchmark proxy | 同上、ticker `1321`（野村 日経225 ETF） | 同上 |
| 週次 candidates | `records/04-candidates/<YYYY>/<MM>/<YYYY-MM-DD>.yaml`（local store） | 機械生成 fact |
| research_memo 判断 ledger | `records/_ledger/research-decisions/<YYYY>-<MM>.jsonl` | 判断時点 fact |
| 実 trade record | `records/06-trades/**/*.md` | execution fact |
| open position benchmark | `baibai-loop-ledger benchmark` | 上記を join |

価格関数の正本は `src/baibai_loop/ledger/tracking.py` `resolve_price_on_or_before`。**自前のスクリプトでも必ずこれと同じセマンティクス**（adjusted close 優先、target 日以前の最新 bar）を使うこと。

## 3. 7 axis backtest 手順

「意味あるバックテスト」を担保する最低限の axis。dated な計測実施では本リストを上から順に走らせる。

### Axis A — screening profile replay（既存 CLI）

`baibai-loop-ledger screening-replay` で profile（`balanced` 等）の forward return を benchmark proxy 比で計測。

```bash
uv run baibai-loop-ledger screening-replay \
  --candidates-root .cache/replay/candidates \
  --regime-lens on --top 10 \
  --out .cache/backtest/<YYYY-MM-DD>-replay.yaml
```

事前準備: 対象週の candidates が手元になければ `baibai-loop-screening run --asof <週> --allow-stale-jpx` で再生成し `.cache/replay/candidates/<YYYY>/<MM>/<YYYY-MM-DD>.yaml` に配置。replay は macro-agnostic で動く。eval cap が cache 最新足を超える horizon は unresolved として集計から除外される。

### Axis B — lane cohorts（既存 CLI）

`baibai-loop-ledger lane-cohorts` で各 playbook lane の **全銘柄** の forward return を集計。recommended queue に乗らない lane の cohort 品質を見る。

### Axis C — selection ablation（既存 CLI）

`baibai-loop-ledger selection-ablation` で ranking 成分（fast_boost / long_hold / lane_rank / strength / diversity 等）と各 lane の `Δfull` を計測。死荷重の検出と lane 順序の検証。

### Axis D — judgment-gate counterfactual（本 runbook 新規）

ledger の research_memo decision に対し、validator の gate（market_regime / market_relative_return / macro_freshness / tactical_exposure）を後付けで適用し、「gate ON だったら何件が defer/starter になり、cumulative rel がどう変わったか」を構築する。

```bash
.venv/bin/python .cache/backtest_multi_axis.py   # 雛形は本 PR (#245) の .cache/ にある
```

- 最低限の出力: actual_pnl / defer_pnl_saved / lenient_50%_pnl
- 出力は `.cache/backtest/<YYYY-MM-DD>-counterfactual.txt` に保存し、reports/ にダイジェスト

### Axis E — bootstrap CI

approved n が 1 桁の段階では mean rel の点推定は不安定。`random.choices` で 10,000 回 resampling し 95% CI を算出。`P(true mean < 0)` を出して「点推定が偶然か」を明示。

### Axis F — opportunity-cost check（deferred / rejected の事後検証）

`outcome ∈ {deferred, rejected}` のその後の forward return を集計し、`share with rel<0` で「見送り判定の正答率」を出す。これがバックテストの **negative control**（gate が単に保守化するだけでなく、有効に作用したかの裏付け）。

### Axis G — regime × lane クロステーブル

`(playbook, regime) -> mean rel` のクロステーブルを出す。regime 別に lane の頑健性が違う前提で観察する (2026-05 観測では valuation-reversion が rally に頑健、cashflow-yield が劣後など)。

## 4. 再現性の手順

1. `task/backtest-<YYYY-MM-DD>` ブランチを切る
2. `data/screening/market.sqlite` の最新足を fetch（`bootstrap-cache`）し、`MAX(traded_at)` を出力に明記
3. `records/04-candidates/` の対象週 YAML が手元になければ `baibai-loop-screening run --asof <週> --allow-stale-jpx` で再生成（出力は `.cache/replay/candidates/` 配下）
4. 上記 7 axis を順に実行し、生 output を `.cache/backtest/<YYYY-MM-DD>-*` に保存
5. ダイジェストを `reports/<YYYY-MM-DD>-backtest.md` に書く（数値・eval cap・サンプル制約を必ず明示）
6. **必ず in-sample / out-of-sample を区別**して書く。同じ期間で hypothesis 生成と検証を行った計測は in-sample であることを明示

## 5. やってはいけないこと（AGENTS.md / philosophy 由来）

- **+15bd 固定 horizon で entry 日が異なる銘柄を平均する**: entry 日違いの群を fair に比較できない。同じ eval cap に揃える
- **未来データを用いた閾値 grid search**: regime 閾値 +3% 等は事前固定し、観測後の最適化はしない
- **小サンプルで点推定だけを記述**: 必ず bootstrap CI または n と分散を併記
- **screening recommended queue だけを backtest にして judgment を測らない**: judgment gate の効果は別 axis（D）で測る
- **counterfactual を語るのに具体的な lenient/strict variant を出さない**: 「もし gate ON なら良くなった」だけでは不可、サイズ強制 100% / 50% の両方を出す

## 6. 直近の実施記録

新規エントリは **(asof / 計測内容 / ダイジェスト / 結論への反映)** の 4 列必須。ダイジェスト本文は `reports/<asof>-*.md` に dated まとめとして残し、本表からリンクする。「結論への反映」が **defer** の場合は理由を 1 行で書く (例: 「サンプル不足、forward 蓄積 +N 件で再評価」)。

| asof | 計測内容 | ダイジェスト | 結論への反映 |
| --- | --- | --- | --- |
| 2026-05 | screening profile replay (balanced/strict/loose × 4 週) | mean rel 4w 全 profile −5.6〜−12.3pt | `balanced` 据え置き |
| 2026-05 | regime lens on/off | 4w ON −4.03pt vs OFF −9.26pt | regime lens を default ON 化 |
| 2026-05 | selection ablation | `evidence_count` / `long_hold` / `prior_suppression` が queue 無変化 | sort 成分整理 (#217) |
| 2026-05 | lane cohorts | `strict-net-cash` / `fcf-yield` lane が top5 不到達、`cash-rich` が baseline +5.32pt で全 lane 中最強 | `strict-net-cash` / `fcf-yield` 削除、`cash-rich` 維持 (PR #246) |
| 2026-06-17 | judgment-gate counterfactual / lane × regime cross / bootstrap CI | approved 8 件 rel −3.45pt、95% CI [−7.60, +0.32]、P(<0)=96% | regime gate を judgment 層に実装 (PR #245) |
| 2026-06-18 | cleanup round 2 (scorecard / structural / breadth / 8w / profile YAML) replay before/after diff | mean rel diff +0.00pt × 3 wave | 削除は forward 計測の no-regression を満たす (PR #248) |
| 2026-06-18 | cleanup round 3 (HorizonReturn.benchmark_return / concentration diagnostics / add_source_coverage helper) replay diff 6 週 (2026-05-01 〜 2026-06-12) | recommended_tickers 完全一致 6/6 週、mean_relative diff +0.00pt | 削除は forward 計測の no-regression を満たす、6 週 multi-period で検証 |
| 2026-06-18 | perf (`yaml_io.safe_load` 全 reader 経由化 + run_replay 内 YAML payload cache / `_insert_bars` 3 module 統合) replay diff 6 週 + wall time 5 runs | wall 17.26s → 2.15s (**-87.5%, 8.03x**); recommended_tickers 6/6 完全一致 + 出力 YAML md5 byte-identical | CSafeLoader を `baibai_loop.yaml_io.safe_load` 経由で全 14 src reader に強制、`run_replay` で current/previous の二重 YAML parse を `payload_cache` で dedup (R5 review 発見の最大 win)。1/2/3/4/6/7/7-rev 候補は実測で defer |
| 2026-06-19 | strategy 改善 5 Phase + Phase 6 fixup (selection_ablation regime bug fix / lane order / sector cap / per_forward gate + history populate / sector 除外拡張 / deterioration & margin & FCF gate / accruals (TTM 修正) & net share issuance signal / back-fill cohort 分離 + typo guard / dead variant 整理 / docs 7 箇所更新) | ablation full 4w mean rel (5 週 apples-to-apples、R1 検証): OLD -9.6pt → +B1 -3.0pt (+6.6pt) → +A1-A3 -1.9pt (+1.1pt, CI 跨ぎ) → Phase 2-5 -1.9pt (+0.0pt full に現れず) **合計 +7.7pt** (うち +6.6pt は ablation 計測 infra bug 修正 = production 挙動不変)、benchmark 規律下 cohort **rel +1.23pt** (全 trade -1.29pt から back-fill 3 件除外で +2.52pt 解像度向上、完全再現) | 計測ベースに採用施策のみ統合、forward 計測経路を維持。Phase 6 で R1-R5 reviewer P0 反映 (per_forward no-op fix / D2 accruals proper TTM / CLI typo guard / docs 7 箇所更新)。残り B3 / E1 / C3 / D1 / D4-D6 / E2 / E4 / E6 / R2 P1 / R3 P1 / R4 P1 / R5 P1 / bootstrap CI は scope creep 回避で次回 PR (reports/2026-06-19-strategy-improvement-progress.md §4 参照) |

ダイジェスト本文は [`../../reports/2026-06-17-trade-strategy-rootcause.md`](../../reports/2026-06-17-trade-strategy-rootcause.md) §13、過去の dated 計測 doc は同レポートで上位概念に統合済み。
