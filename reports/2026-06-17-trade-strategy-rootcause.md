---
title: "Trade strategy root-cause analysis (2026-06-17)"
summary: "実トレード 8 件の市場対比アンダーパフォーマンスを定量分解し、原因を market regime と judgment gate の欠落に特定。entry_preflight に regime gate を実装し、6/17 時点の bargain 4 候補を提示する。"
doc_type: reference
status: active
last_reviewed: 2026-06-17
---

# Trade strategy root-cause analysis (2026-06-17)

## 1. 問い

2026-05〜06 の実トレード 8 件が「マイナスが多い」という感覚に対し、根本原因が **(A) 戦略そのもの / (B) スクリーニング甘さ / (C) macro timing 誤り** のどれかを定量で特定する。

## 2. ledger / benchmark CLI が出した一次事実

`baibai-loop-ledger benchmark --asof 2026-06-16 --proxy 1321` の出力（eval cap は SQLite 最新 2026-06-12 bar）:

```
TOTAL notional=1,291,400 pnl=+25,800 ret=+2.00% bm=+3.29% rel=-1.29pt
```

- 絶対損益は +25,800 円 / +2.00%（プラス）
- **市場（1321 = 日経225 ETF proxy）対比 −1.29pt**（アルファ負）
- ポジション別 rel: 8 件中 5 件マイナス（9682 −7.74pt / 9470 −5.69pt が大敗、8255 +4.35pt が大勝）

「マイナス感」の正体は絶対損益ではなく、最高値圏に上昇した市場に **+3.29% で勝てなかったこと**。

## 3. 判断 (research_memo) ledger の forward tracking

`records/_ledger/research-decisions/2026-05.jsonl` 全 14 件と 2026-06 の 3 件を +15bd tracking で集計（手元 sqlite に対し `adjustment_close` で同一 basis）:

| 切り口 | n | mean rel (vs 1321) | 勝率 (rel>0) |
|---|---:|---:|---:|
| outcome=approved | 5 | **−7.74pt** | **0 %** |
| outcome=deferred | 7 | −5.03pt | 29 % |
| outcome=rejected | 2 | −6.37pt | 0 % |
| playbook=sales-discount-growth | 3 | **−8.87pt** | 0 % |
| playbook=cashflow-yield-discount | 7 | −5.83pt | 14 % |
| playbook=fcf-yield-discount | 4 | −4.80pt | 25 % |
| macro_fit=mixed | 3 | **−12.39pt** | 0 % |
| macro_fit=neutral | 5 | −3.64pt | 20 % |
| decision_effect=proceed | 13 | −6.31pt | 15 % |

deferred/rejected の **78 % は事後も rel<0**（見送り判定はおおむね正解）。買った 5 件は全て rel<0。「買う判断」の質に問題が集中する。

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

## 5. screening 推奨 vs 人間 judgment（フェア比較）

「+15bd 固定 horizon」では entry 日が異なる銘柄を不公平に比較してしまうため、entry → 同一 eval cap（2026-06-12）で再計算:

| | mean relative | n |
|---|---:|---:|
| 実トレード（人間選定 5 件） | **−1.37pt** | 5 |
| 同週 screening recommended top5（regime-on） | **−8.77pt** | 10 |

**人間 judgment は screening 機械ランキングより約 +7pt 優位**。screening の top5 は fast-dislocation 寄りで 6619 −10 % / 1899 −14 % など落ちるナイフが混入。人間は IR で AI 耐性・shareholder return・cashflow を確認し defensive/quality に寄せた選定で底堅さを示した。

つまり「screening を強化して人間判断に合わせる」必要はない。**両者とも市場（+3〜+14 %）に負ける rally 局面で逆張り買いを続けた構造**が真因。

## 6. 既存改善との関係

`docs/screening/regime-lens-replay-2026-05.md` で 2026-05 4 週の lens ON/OFF を replay 済み（4w ON −4.03pt vs OFF −9.26pt、+5.23pt 改善）。`docs/screening/selection-ablation-2026-05.md` も済。**しかし lens は ranking のみ（lens, not gate）で、研究判断・トレード判断には接続されていなかった**。これが本 PR の対象差分。

## 7. 原因確定

| 仮説 | 評価 |
|---|---|
| (A) 戦略そのもの誤り | **否**。philosophy 柱 2 は「過剰に売られた銘柄を追い風で底値拾い」。前提が崩れた局面で運用したのが問題で戦略自体ではない |
| (B) スクリーニング甘さ | **副次**。screening top5 は人間選定より劣後（rel −8.77 vs −1.37pt）。改善余地はあるが本件損失の主因ではない |
| (C) macro timing | **主因確定**。5 月全週 risk_on_rally で逆張り買い継続。regime 判定機能はあるのに judgment を縛らない設計 |

## 8. 改善: entry_preflight に regime gate を実装

`src/baibai_loop/validate/research/preflight.py` および `records/_schemas/research.json` を変更:

1. `entry_preflight.market_regime` field を追加（regime / benchmark_return_20d / benchmark_ticker / evaluated_on）。2026-06-17 以降の approved research に必須化。
2. `regime: risk_on_rally` は **proceed の hard_trigger**。proceed 禁止 → starter/exception/defer に降格。
3. `risk_on_rally × action ∈ {starter, exception} × near_term_catalyst=false × exception_basis に near_term_catalyst/low_correlation なし` で **warning** 発火 — 規律 nudge として defer を促す。
4. `_REGIME_GATE_EFFECTIVE_DATE = 2026-06-17` で既存記録は非破壊（4432=6/16 含む既存 0 件が error にならないことを validate で確認）。

### 運用テスト（out-of-sample）

直近の 4432 research に `market_regime: risk_on_rally / benchmark_return_20d: 0.0748` を追記して validate:

```
[warning] 4432: research.entry-preflight-rally-contrarian @ entry_preflight.market_regime —
  risk_on_rally regime with no near_term_catalyst and no low_correlation basis:
  a contrarian value entry structurally lags a trending index — consider defer
```

新ゲートは意図通り発火。約定済みのため warning（error にはしない）。次回以降の同状況では starter を選ぶ前に defer を考える規律になる。

## 9. 検証

```
baibai-loop-validate: 37 files, 0 error, 5 warning（既存 4 + regime warning 1）
ruff format --check . : 148 files already formatted
ruff check .          : All checks passed
mypy                  : 101 source files, no issues
pytest                : 590 passed, 2 skipped
```

新規テスト 7 件（regime ゲート on/off、proceed 禁止、starter contrarian warning、catalyst/low_correlation waiver、gate-date 必須化、不明 regime label 拒否）。

## 10. 6/17 時点 bargain 4 候補（PR 成果物の一部）

最新 weekly screen `records/04-candidates/2026/06/2026-06-12.yaml`（1,788 銘柄）に対し、以下の決定論的フィルタとスコアで絞り込み（`.cache/select_4_bargains_20260617.py`、AGENTS.md / philosophy 柱 5 の「fact 層を狭めない、絞り込みは分析時パラメータ」に従う）:

- 流動性: avg_turnover_oku ≥ 1.0
- 規模/履歴: market_cap_oku ≥ 200・listing_span ≥ 750・price_history_coverage_750d ≥ 0.97
- valuation 規律: PER (3, 18] / PBR ≤ 2.5 / P/S ≤ 3
- AI 耐性 leaning: SoR / 規制 / インフラ / 内需 service を含む sector_33 集合
- quality lane: evidence_hits に cashflow-yield-discount / fcf-yield-discount / cash-rich-asset-discount のいずれか
- rally-contrarian discipline: pc20 ≤ +2 %
- 既存保有 / 直近 research 銘柄を除外、sector_33 同一は最大 2 銘柄

| rank | ticker | name | sector_33 | PER | PBR | P/S | pc20 | OCF yield (TTM) | lanes |
|---:|---|---|---|---:|---:|---:|---:|---:|---|
| 1 | 5989 | エイチワン | 金属製品 | 3.82 | 0.53 | 0.20 | −5.99 % | 67.6 % | cash-rich + cashflow |
| 2 | 9022 | 東海旅客鉄道（JR 東海） | 陸運業 | 5.89 | 0.63 | 1.68 | −7.40 % | 22.2 % | cashflow |
| 3 | 3443 | 川田テクノロジーズ | 金属製品 | 7.14 | 0.63 | 0.55 | −9.10 % | 24.1 % | cashflow |
| 4 | 9997 | ベルーナ | 小売業 | 7.85 | 0.60 | 0.42 | +1.18 % | 20.2 % | cashflow |

この 4 候補は **screening 機械スクリーンの上位** であり、IR 深掘り（一次決算、配当方針、catalyst、AI 耐性論証）を経て 1 銘柄に絞ることが skill `ai-value-bargain-selection` の運用前提。今回は PR 成果物として「現時点の機械スクリーン上の bargain 候補」を提示するに留め、実約定は IR 確認後の別 PR とする。

なお新 regime gate の下では「6/17 時点 1321 20bd return ≈ +5.7 %（risk_on_rally）」のため、これら 4 候補を act_now で買う場合も entry_preflight は **starter 以下** に降格、catalyst なしの場合は warning 発火。

## 11. 結論

- **戦略は壊れていない、screening 甘さでもない、macro timing（rally 局面で逆張り買い）が主因**
- regime 判定機能は存在したが ranking lens に留まり judgment を縛れていなかった
- entry_preflight に regime gate を入れることで「rally × catalyst なし逆張り」を規律化
- 既存改善（regime-lens-replay-2026-05、selection-ablation-2026-05）と本 PR で screening の ranking と judgment の両面に regime 規律が通る

## 12. 限界

- 計測サンプルは 5 月の 4 週 / approved 5 件と小さい。「2026-05 で有効」以上の結論を出さず、forward 運用で再評価する
- regime 閾値（+3 %）は事前固定、grid search していない。本 PR でも閾値は変更しない
- 4 候補は機械スクリーン上位であり、IR 深掘り未実施。実約定の最終判断には ir-research skill での一次確認が必要
