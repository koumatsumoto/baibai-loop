# 保存済み旧評価の読取

旧ER/FV・四Approachの再推計/再選抜、較正の再生成/再配布は廃止済み。保存run・旧Selection・同梱contextを当時の値/identity/時刻/欠測のまま読み、現行モデル・候補・L3評価/Reviewを代行させない。store/原本は削除しない。

exact `run_revision_id` の `historical-evaluation run-show` / `candidates show` を使う。旧Selectionも保存分析を投影し、現在のrules/推計で埋めない。

<a id="machine-valuation"></a>
## 保存済み機械ER/FVの意味

旧E[r]は年率比較のprior、FVは機械比較anchorで、個社価値・採否・保有期間ではない。3y/5yは実現値の比較窓で推計期間ではなく、[L3の条件付き価値](stock-assessment.md#scenario-arithmetic)と分ける。

<a id="carry-policy"></a>
- carry欠測の0寄与≠無配/株数不変。グロス株数減少≠自己株取得cash/未消化枠/将来還元。時点・basis・欠測を補わない。
- 業種中央値のmarket fallback≠同業比較。現在fundamentalsを固定した自己価格レンジ≠当時の倍率履歴。3FY平均EPSは算術平均で経済的正常利益ではない。

<a id="er-level-distribution-context"></a>
## 凍結した歴史分布

[同梱YAML](../../engine/src/baibai_engine/machine_analysis/historical_evaluation/context/published/er-level-calibration-latest.yaml)は当時の記録で、日付/hashの更新や再生成で有効に見せない。[接続owner](../../engine/src/baibai_engine/investment_judgment/workflow/calibration_context.py)はidentity/有効期間が合う旧Selectionだけに添え、期限切れ・不一致・不足を保つ。新GeneratedResearchSelectionは `not_applicable`。

帯は宣言予測母集団の観測分布で、未評価≠0損益。宣言/観測の月数・企業数・重みを分け、8.5%帯を[L3要求率](../doctrine.md#required-return-policy)へ連動させない。price-only/FY配当込みの母集団差を配当効果とせず、FY配当を支払/権利落ちcash ledgerとしない。

固定v43は粗い順位の研究材料に留める。退出・配当・PIT・実行価格の制約から数値較正/買い閾値へ[不採用](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-23-v43-evidence-decision/report.md)で、formal evidence不足/負の結果を効果・採用・利益の認定にしない。

## 現行判断を将来比較へつなぐ前の診断

旧較正とは別の[現行判断の比較前診断](screening-runtime.md#decision-cohort-diagnostic)を使う。command・対象/分母・時点と証明しない範囲はリンク先に集約する。
