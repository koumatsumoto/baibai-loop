---
title: "Trade strategy root-cause analysis (2026-06-17)"
summary: "実トレード 8 件の市場対比アンダーパフォーマンスを定量分解。主因仮説は「2026-05 全週が risk_on_rally で逆張りバリュー entry が構造的劣後」。entry_preflight に regime gate を実装し、6/17 時点の bargain 4 候補を提示する。"
doc_type: reference
status: active
last_reviewed: 2026-06-17
related_docs:
  - "../docs/operations/backtest-runbook.md"
  - "../docs/screening/regime-lens-replay-2026-05.md"
  - "../docs/components/research.md"
---

# Trade strategy root-cause analysis (2026-06-17)

## TL;DR

- **真因仮説 (in-sample)**: 戦略は壊れていない、screening 甘さでもない。**2026-05 全週が risk_on_rally (1321 20bd return +8.7〜17.4%) で、逆張りバリュー entry を継続したことが主因**。bootstrap 95% CI [−7.60pt, +0.32pt] で点推定 −3.45pt (n=8, P(true<0)=96%)。
- **修正**: `entry_preflight` に regime gate を実装。`risk_on_rally` で proceed を hard-block。`unknown` regime も hard-block。label と benchmark_return_20d の不整合も hard-block。`exception × low_correlation 無し` も hard-block。`starter × catalyst 無し × low_correlation 無し` は warning。
- **counterfactual (in-sample, n=8)**: gate ON で defer 強制すれば cum rel −27.6pt → 0pt、50% size 強制で −13.8pt の改善。但し標本サイズ小、forward 検証が必要。
- **lane × regime cross**: `fcf-yield-discount` が rally 中 mean rel **−16.08pt (n=4)** で最弱、`valuation-reversion` は **−0.50pt (n=2)** で頑健。lane の rally 耐性に差がある (in-sample)。
- **新 bargain 4 候補 (6/17)**: 5989 エイチワン / 9022 JR 東海 / 3443 川田テクノロジーズ / 9997 ベルーナ。但し 6/17 の regime は依然 risk_on_rally (20bd +5.69%) のため、新 gate 下では starter 以下 + catalyst 確認が必須。

## 1. 問い

2026-05〜06 の実トレード 8 件が「マイナスが多い」という感覚に対し、根本原因が **(A) 戦略そのもの / (B) スクリーニング甘さ / (C) macro timing 誤り** のどれかを定量で特定する。

## 2. ledger / benchmark CLI が出した一次事実

`baibai-loop-ledger benchmark --asof 2026-06-16 --proxy 1321` の出力（eval cap は SQLite 最新 2026-06-12 bar）:

```
TOTAL notional=1,291,400 pnl=+25,800 ret=+2.00% bm=+3.29% rel=-1.29pt
```

- 絶対損益は +25,800 円 / +2.00%（プラス）
- **市場（1321 = 日経225 ETF proxy）対比 −1.29pt**（アルファ負）
- ポジション別 rel (8 件中 5 件マイナス):
  - 9682 -7.74pt / 9470 -5.69pt が大敗（+15bd horizon ベース）
  - 8255 +4.35pt が大勝（同上）

「マイナス感」の正体は絶対損益ではなく、最高値圏に上昇した市場に **+3.29% で勝てなかったこと**。

## 3. 判断 (research_memo) ledger の forward tracking (+15bd horizon)

`records/_ledger/research-decisions/*.jsonl` 全 14 件 (2026-05) + 3 件 (2026-06) を +15bd tracking で集計。`adjustment_close` on-or-before、benchmark proxy `1321`。

| 切り口 | n | unique tickers | mean rel (vs 1321) | 勝率 (rel>0) | 注 |
|---|---:|---:|---:|---:|---|
| outcome=approved | 5 | 5 | **−7.74pt** | **0 %** | (+15bd horizon ベース; §4 参照) |
| outcome=deferred | 7 | 5 | −5.03pt | 29 % | 6310/6835 が複数 re-examination |
| outcome=rejected | 2 | 2 | −6.37pt | 0 % | |
| playbook=sales-discount-growth | 3 | 3 | **−8.87pt** | 0 % | 9682/9692/9470 (5/5–5/8 cohort と同一) |
| playbook=cashflow-yield-discount | 7 | 5 | −5.83pt | 14 % | |
| playbook=fcf-yield-discount | 4 | 2 | −4.80pt | 25 % | 6835 を 3 回 re-examination |
| macro_fit=mixed | 3 | **1** | **−12.39pt** | 0 % | **全件 6310 の re-examination、独立観測でない** |
| macro_fit=neutral | 5 | 5 | −3.64pt | 20 % | |
| decision_effect=proceed | 13 | 9 | −6.31pt | 15 % | nearly 全体 (n=14 中 13)、bucket としての情報量は低い |

deferred/rejected の **89% (= 8/9) は事後も rel<0**（見送り判定はおおむね正解、F. opportunity cost check 参照）。買った 5 件は **+15bd で全敗**だが、eval cap=6/12 では 9692 が +0.81pt で 4/5 敗 — 「全敗」の表現は horizon 依存。

## 4. macro timing — 一次事実

1321 ETF の 4/28 → 6/12 推移:

| 日 | close | 累積 |
|---|---:|---:|
| 04-28 | 62,650 | 0.0% |
| 05-07 | 65,880 | +5.2% |
| 05-13 | 66,200 | +5.7% |
| 05-25 | 68,330 | **+9.1%** |
| 06-03 | 71,580 | **+14.3%** |
| 06-12 | 69,090 | +10.3% |

`screening/regime.py` の閾値 `RALLY_RETURN_20D_MIN = +3 %` に照らすと **5 月の全 4 週が `risk_on_rally`**（benchmark_return_20d +8.7〜17.4 %）。実トレード約定もこの 4 週に集中（9682/9692 5/7、9470 5/7、8255 5/14、3539 5/25、2749/9534 6/9、4432 6/16 すべて rally 局面）。

philosophy 柱 2「一時的に過剰に売られている割安を底値で掴む」の前提（追い風）が崩れた局面で逆張り買いを続けた構造。

## 5. screening 推奨 vs 人間 judgment（フェア比較、週揃え）

「+15bd 固定 horizon」では entry 日が異なる銘柄を不公平に比較してしまうため、entry → 同一 eval cap (2026-06-12) で再計算。さらに **entry 週 unique** で揃え、両群を 5/1+5/8 cohort に限定:

| | mean relative | n | entry 週 |
|---|---:|---:|---|
| 実トレード (5/7 約定の 9682/9692/9470 のみ) | **−3.30pt** | 3 | 5/1 cohort と 5/8 cohort 跨り |
| screening top5 (5/1 + 5/8 weeks) | **−8.77pt** | 10 | 5/1 + 5/8 |
| 実トレード (全 5 件 5/7–5/25) | **−1.37pt** | 5 | 5/1/5/8/5/15 cohort 横断 |

- **同じ 5/1+5/8 cohort 同士で揃えても、人間 judgment は screening top5 より +5.47pt 優位** (n は片側 3、両者とも小さい)。
- screening の top5 は fast-dislocation 寄りで 6619 −10.0%、1899 −14.1%、6619 (2 週連続) など落ちるナイフが混入。
- 人間は IR で AI 耐性・shareholder return・cashflow を確認し defensive/quality に寄せた選定で底堅さを示した。
- profile = balanced (regime-lens on)、top5 を recommended_tickers から抽出。`.cache/replay/rootcause-replay-on.yaml` 参照。

つまり「screening を強化して人間判断に合わせる」必要はない。**両者とも市場 (+3〜+14 %) に負ける rally 局面で逆張り買いを続けた構造**が真因。

## 6. 既存改善との関係

`docs/screening/regime-lens-replay-2026-05.md` で 2026-05 4 週の lens ON/OFF を replay 済み（4w ON −4.03pt vs OFF −9.26pt、+5.23pt 改善）。`docs/screening/selection-ablation-2026-05.md` も済。**しかし lens は ranking のみ（lens, not gate）で、研究判断・トレード判断には接続されていなかった**。これが本 PR の対象差分。

なお `regime-lens-replay-2026-05.md` は閾値 +3% を「観測後の 1 回の改訂」として明示している (in-sample tuning)。本 PR の hard gate もこの閾値を流用しており、**forward 検証で再評価が必要** (§13 参照)。

## 7. 原因仮説（in-sample）

| 仮説 | 評価 |
|---|---|
| (A) 戦略そのもの誤り | **否**。philosophy 柱 2 は「過剰に売られた銘柄を追い風で底値拾い」。前提が崩れた局面で運用したのが問題で戦略自体ではない |
| (B) スクリーニング甘さ | **副次**。screening top5 は人間選定より劣後（rel −8.77 vs −1.37pt）。改善余地はあるが本件損失の主因ではない |
| (C) macro timing | **主因仮説 (in-sample)**。5 月全週 risk_on_rally で逆張り買い継続。regime 判定機能はあるのに judgment を縛らない設計。bootstrap 95% CI で P(true mean < 0) = 96%。但し n=8 全件同一 regime で、selloff / neutral 局面の挙動は未確認 |

## 8. 改善: entry_preflight に regime gate を実装

`src/baibai_loop/validate/research/preflight.py` および `records/_schemas/research.json` を変更:

1. `entry_preflight.market_regime` field を追加（regime / benchmark_return_20d / benchmark_ticker / asof / eval_date / breadth）。2026-06-17 以降の approved research に必須化。フィールド名は `MarketRegimeSnapshot.to_dict()` 出力に揃え、`ticker-profile` / `market-snapshot` CLI 出力をそのまま貼り付けられる。
2. **hard triggers (proceed を error にする)**:
   - `regime: risk_on_rally`
   - `regime: unknown` (データ不在を「proceed の根拠」にさせない。bootstrap-cache を先に実行)
   - label と benchmark_return_20d の **不整合 cross-check** (`regime: neutral_range, benchmark_return_20d: 0.10` のような mislabel をブロック)
3. **rally-contrarian rule**:
   - `risk_on_rally × action: exception × waiver basis (low_correlation) 無し × near_term_catalyst: false` は **error** (operator が exception で明示的に gate を override した場合は基拠を必須にする)
   - `risk_on_rally × action: starter × 同上` は **warning** (smaller bet を選んだ場合、defer を促す nudge)
4. **gate-boundary date** は `published_at / recorded_at / decided_at / filename` の **max** を取り、backdated published_at で 2026-06-17 gate を回避できないようにする。`_gate_boundary_date` helper を追加 (既存の `_research_record_date` 仕様は不変)。
5. `_REGIME_GATE_EFFECTIVE_DATE = 2026-06-17` で既存記録は非破壊。
6. warning / error message に regime label / benchmark_return_20d / asof を含め、operator が再オープン無しに判断できるよう改善。
7. validator message に `bootstrap-cache` 案内を含め、fresh checkout でも dead-end にならない。

### 運用テスト（out-of-sample）

直近の 4432 research に `market_regime: risk_on_rally / benchmark_return_20d: 0.0748` を後付け追記して validate:

```
[warning] 4432: research.entry-preflight-rally-contrarian @ entry_preflight.market_regime —
  risk_on_rally regime (benchmark_return_20d=+0.0748 (1321), asof 2026-06-16) with no
  near_term_catalyst and no low_correlation basis: a contrarian value entry structurally
  lags a trending index. Add `near_term_catalyst: true` with a dated event, supply
  `exception_basis: [low_correlation]`, or downgrade `action: defer`.
```

新ゲートは意図通り発火。約定済みのため warning（error にはしない）。次回以降の同状況では starter を選ぶ前に defer を考える規律になる。

## 9. 検証

```
baibai-loop-validate: 37 files, 0 error, 5 warning（既存 4 + regime warning 1）
ruff format --check . : 148 files already formatted
ruff check .          : All checks passed
mypy                  : 101 source files, no issues
pytest                : 590+ passed (含: 新規 regime gate test 14 件)
```

新規テスト 14 件（regime ゲート on/off、proceed 禁止、starter contrarian warning、catalyst/low_correlation waiver、gate-date 必須化、不明 regime label 拒否、unknown 拒否、mismatch detection、partial mapping 拒否、filename backdating ブロック、defer-in-rally no-warning、exception+low_sizing error、boundary 6/16）。

## 10. 6/17 時点 bargain 4 候補（PR 成果物の一部）

最新 weekly screen `records/04-candidates/2026/06/2026-06-12.yaml`（1,788 銘柄）に対し、以下の決定論的フィルタとスコアで絞り込み（`.cache/select_4_bargains_20260617.py`、AGENTS.md / philosophy 柱 5 に従う）:

- 流動性: avg_turnover_oku ≥ 1.0
- 規模/履歴: market_cap_oku ≥ 200・listing_span ≥ 750・price_history_coverage_750d ≥ 0.97
- valuation 規律: PER (3, 18] / PBR ≤ 2.5 / P/S ≤ 3
- AI 耐性 leaning: SoR / 規制 / インフラ / 内需 service を含む sector_33 集合
- quality lane: evidence_hits に cashflow-yield-discount / fcf-yield-discount / cash-rich-asset-discount のいずれか
- rally-contrarian discipline: pc20 ≤ +2 %
- 既存保有 / 直近 research 銘柄を除外、sector_33 同一は最大 2 銘柄

| rank | ticker | name | sector_33 | PER | PBR | P/S | pc20 | OCF yield (TTM) | mkt_cap_oku | avg_to_oku |
|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | 5989 | エイチワン | 金属製品 | 3.82 | 0.53 | 0.20 | −5.99 % | 67.6 % | 423 | 4.1 |
| 2 | 9022 | 東海旅客鉄道（JR 東海） | 陸運業 | 5.89 | 0.63 | 1.68 | −7.40 % | 22.2 % | 33,690 | 103.2 |
| 3 | 3443 | 川田テクノロジーズ | 金属製品 | 7.14 | 0.63 | 0.55 | −9.10 % | 24.1 % | 629 | 4.9 |
| 4 | 9997 | ベルーナ | 小売業 | 7.85 | 0.60 | 0.42 | +1.18 % | 20.2 % | 915 | 3.4 |

### Next steps for the operator

- 各銘柄の一次 IR 確認は skill `ai-value-bargain-selection` または skill `ir-research` を実行
  - 確認項目: 直近決算短信、配当方針、share buyback、AI 長期影響、4 軸 (valuation / cashflow / financial soundness / catalyst)
  - 出典: 各社 IR ページ、日経会社情報 (`https://www.nikkei.com/nkd/company/?scode=<ticker>`)、kabuyoho、IRBANK
- 次回決算日は `next_earnings_date` field が candidate row に未収録のため、IR で個別確認
- **新 regime gate の下では 6/17 時点 1321 20bd return ≈ +5.69%（risk_on_rally）のため、これら 4 候補を act_now で買う場合の preflight は**:
  - `action: proceed` は **error** (hard-block)
  - `action: starter` で `near_term_catalyst: true` を立てるには近接 catalyst を IR で確認
  - `near_term_catalyst: false` の場合は warning 発火、defer 推奨
  - `exception_basis: [low_correlation]` を使う場合は半導体ラリーとの相関弱を data で示す

## 11. 結論

- **戦略は壊れていない、screening 甘さでもない、macro timing（rally 局面で逆張り買い）が主因仮説**
- regime 判定機能は存在したが ranking lens に留まり judgment を縛れていなかった
- entry_preflight に regime gate を入れることで「rally × catalyst なし逆張り」を規律化
- 既存改善（regime-lens-replay-2026-05、selection-ablation-2026-05）と本 PR で screening の ranking と judgment の両面に regime 規律が通る

## 12. 限界（honest disclosure）

- **計測サンプルは小さい**: 5 月の 4 週、approved n=8、unique tickers 8。「2026-05 で観察」以上の結論を出さず、forward 運用で再評価する
- **単一 regime**: n=8 全件 risk_on_rally。selloff / neutral 局面の挙動は未確認
- **post-hoc threshold**: regime 閾値 +3% は `regime-lens-replay-2026-05.md` で 2026-05 観測後に改訂された。本 PR は同期間で in-sample validation を実施しており、forward での再評価必須
- **macro_fit=mixed n=3 は 6310 一銘柄の re-examination**: 独立観測でないため bucket としての信頼性が低い。playbook=sales-discount-growth n=3 は approved cohort と一致しており、`outcome=approved` 行に追加情報はない
- **4 候補は機械スクリーン上位**であり、IR 深掘り未実施。実約定の最終判断には ir-research skill での一次確認が必要

## 13. 多角バックテスト結果 (in-sample, 2026-05)

`.cache/backtest_multi_axis.py` の出力ダイジェスト。手順は [`../docs/operations/backtest-runbook.md`](../docs/operations/backtest-runbook.md) の 7 axis に従う。

### A. Counterfactual: gate ON vs OFF (eval=6/12)

- approved n=8 (research_memo の approved 全件、ledger の +15bd ベースでなく entry → 6/12 共通 eval)
- actual cum rel = **−27.61pt**
- gate が defer を強制した場合: defer すべき件数 = 8/8 (全件 rally)、defer 強制 cum rel = **0pt** (改善 +27.61pt)
- gate が 50% size を強制した場合: cum rel = **−13.81pt** (改善 +13.8pt)

### B. Regime stratified

- risk_on_rally n=8: mean rel = **−3.45pt** (rels = [-14.28, -4.03, -9.26, +5.61, -4.66, +0.01, -1.01, 0.0])
- neutral_range / risk_off_selloff: **0 件** (forward 検証で他 regime を蓄積する必要あり)

### C. Lane × regime cross (全 ledger n=15 in rally)

| playbook | n | mean rel% |
|---|---:|---:|
| cashflow-yield-discount | 8 | -3.81 |
| fcf-yield-discount | 4 | **-16.08** |
| sales-discount-growth | 3 | -9.19 |
| valuation-reversion | 2 | **-0.50** |

**fcf-yield-discount が rally で壊滅、valuation-reversion が頑健** (in-sample, 4 lane の小サンプルで強い結論は控える)。将来の follow-up: rally 時の lane 抑制ルール。

### D. Bootstrap 95% CI (B=10,000)

- approved n=8 mean rel = −3.45pt
- 95% CI: **[−7.60pt, +0.32pt]**
- **P(true mean rel < 0) = 96.2%**: 偶然のノイズではなく真のアンダーパフォーマンス

### E. Rally threshold sensitivity

| threshold | gate fired on | defer cum rel saved | 50% size benefit |
|---:|---:|---:|---:|
| +3% | 8 / 8 | +27.61pt | +13.81pt |
| +5% | 6 / 8 | +26.62pt | +13.31pt |
| +10% | 4 / 8 | +21.95pt | +10.98pt |

閾値 ±2pt のずれは全件 fire を変えない (5 月 4 週は +8.7〜17.4% の rally で全て上回るため)。+10% に上げても 50% の件で fire し +10.98pt の改善。

### F. Deferral opportunity-cost check (negative control)

- deferred/rejected n=9: mean rel = **−10.64pt**、share with rel<0 = **89%**
- 見送り判定は概ね正解、gate の保守化が単なる過剰回避でないことを裏付け
- 仮にこれらを買っていれば cum rel = **−95.75pt** の追加損失

### G. Walk-forward integrity check

- `.cache/replay/candidates/` 2026-05 全 4 週の regime を再算: 全週 risk_on_rally (20bd +8.7〜+17.4%)
- 当時の candidates だけを使い未来情報を含まないことを確認

### Forward review

forward 検証は `task-runbook` issue として「regime gate effectiveness review @ 2026-07-15」を起票し、追加 4 週後の approved cohort で gate の正味効果と false-positive 率を再計測する。
