# 2026-06-21 lane scorecard — C0 評価方法論と C0-gate 結果

Issue #254 の C0（全候補 backtest の定量評価戦略）と C0-gate（既存 tool での安価検証）の計測記録。`baibai-loop-ledger lane-scorecard` を新設し、screening の lane が「選定価値を生むか（keep / kill / review）」を CI 付きで判定できることを検証した。

## 方法論（C0）

- **評価単位（主軸）= 候補 / lane-cohort 大N**。`lane-cohorts` の per-week per-lane 集計を週跨ぎで pool し、個別候補の forward relative（return − 日経225 ETF proxy 1321）を母数にする。
- **指標**: pooled mean_relative、median、bootstrap 95% CI、P(mean<0)、win率、all_candidates baseline 比 edge。
- **意思決定接続（必須）**: lane の CI を baseline（全候補平均）と比較し、
  - `keep`: CI が baseline より上（選定価値あり）
  - `kill_candidate`: CI が baseline より下（平均以下の選定）
  - `review`: CI が baseline を跨ぐ、または `n < min_resolved`（母数不足）
  - `baseline`: all_candidates 行
- **§9 規律**: bootstrap seed 固定・判定 cut point 固定・grid search なし。forward-only（target が eval cap を超える週は不参加、価格は on/before 解決）。baseline は大N・低分散の固定参照として扱い、edge CI は lane CI を baseline 分シフトしたもの。

## C0-gate 結果（6 asof: 2026-05-01 / 05-08 / 05-15 / 05-29 / 06-11 / 06-12）

market.sqlite の最新足が ~06-12 のため、forward が解決するのは主に 05 月 asof。pooled n は baseline で 4361(1w) / 2821(4w)。

| lane | h | n | mean_rel | edge vs baseline | 95% CI | decision |
| --- | --- | ---: | ---: | ---: | --- | --- |
| all_candidates | 4 | 2821 | −9.13% | +0.00 | [−9.61, −8.61] | baseline |
| **cash-rich-asset-discount** | 4 | 265 | −7.03% | **+2.10pt** | [−8.21, −5.75] | **keep** |
| **cashflow-yield-discount** | 4 | 1161 | −7.99% | **+1.14pt** | [−8.59, −7.38] | **keep** |
| sales-discount-growth | 4 | 1152 | −9.78% | −0.65 | [−10.48, −9.03] | review |
| valuation-reversion | 4 | 871 | −10.02% | −0.89 | [−11.06, −8.89] | review |
| fcf-yield-discount | 4 | 14 | — | — | — | review (underpowered) |
| strict-net-cash-discount | 4 | 19 | — | — | — | review (underpowered) |

baseline 自体が大きく負（この 6 週は screening universe が指数に劣後＝半導体主導ラリー局面）だが、**lane の相対比較は有効**。

## 検証（多角的・実装の妥当性）

- **既知結果の独立再現**: cash-rich-asset-discount が baseline 比で最強（keep）。`backtest-runbook.md §6` の 2026-05 lane-cohorts「cash-rich が baseline +5.32pt で全 lane 中最強」と方向一致。config の `research_selection_lane_order` 先頭が cash-rich なのも整合。
- **整合性**: scorecard の pooled n（4361/2821）が `lane-cohorts` の週次 resolved 合計と完全一致。pooling は正しい。
- **決定性**: bootstrap を canonical sorted population ＋ 固定 seed にし、2 回実行で出力完全一致（pool の set 反復順による非決定性のバグを検出・修正）。
- **感度頑健性**: min_resolved=30/50・bootstrap=2000/10000 で keep/kill/review 判定不変。horizon 2→3→4 で cash-rich の edge が単調増加（+0.75 → +1.69 → +2.10pt）＝coherent。
- **テスト**: `tests/test_ledger_cohort_scorecard.py` 12 件（bootstrap 決定性・順序非依存・判定境界・統合 keep/kill/baseline・reproducibility・underpowered）green。`lane-cohorts` 既存テスト no-regression。

## C1（性能効率）の所見

- 6 asof の scorecard 実行は **約 10 秒**。現スケールでは効率化は不要。
- 効率の爆発は scorecard 側でなく **候補再生成（`run --asof` の full-universe 取得）を多 asof で繰り返す側**。候補が既にあれば評価は速い（レビュー指摘と一致）。
- よって C2（on-demand 取得）は **多 asof スケールが必要になるまで defer**。現状の candidate-level 大N 評価は 6 週で n≈2800–4400 と既に十分な母数。

## スコープと次

- **完了**: C0 評価方法論 ＋ C0-gate（actionable な lane 判定を実証）＋ テスト ＋ 多角検証。
- **defer**: C2 性能効率（現スケール不要、多 asof で再評価）。
- **次**: C3 proposal relevance overlay（recommended queue＝実取引提案単位、小N・CI-gated・directional。候補大N の robust 結論と分離記載）。

## 制約（受け入れ）

過去 asof backtest は PIT 非完全（master / 価格調整 / JPX は latest-snapshot）。backtest は方向性の検証であり真値ではない（`design-principles.md` §9）。
