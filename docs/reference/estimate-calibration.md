# 保存済み旧評価の読取

旧ER/FV・四Approachの再推計/再選抜、較正の再生成/再配布と[専用閲覧](../../jobs/OPERATIONS.md#保存済み旧runを読む)は終了。旧run・保存Researchの根拠は元の値・identity・時刻・欠測を保ち、現行候補やL3評価に読み替えない。

<a id="machine-valuation"></a>
## 保存済み機械ER/FVの意味

旧E[r]は年率比較のprior、FVは機械比較anchorで、個社価値・採否・保有期間ではない。3y/5yは実現値の比較窓で推計期間ではなく、[L3の条件付き価値](stock-assessment.md#scenario-arithmetic)と分ける。

<a id="carry-policy"></a>
- carry欠測の0寄与≠無配/株数不変。グロス株数減少≠自己株取得cash/未消化枠/将来還元。時点・basis・欠測を補わない。
- 業種中央値のmarket fallback≠同業比較。現在fundamentalsを固定した自己価格レンジ≠当時の倍率履歴。3FY平均EPSは算術平均で経済的正常利益ではない。

<a id="er-level-distribution-context"></a>
## 凍結した歴史分布

[2026-10-22の分布](archive/er-bands-2026-10-22.yaml)と[以前の記録](archive/er-bands-2026-09-24.yaml)は当時の資料である。現行の候補は旧機械E[r]を出さない。旧銘柄を帯に再割当てするCLI・Python配布への同梱は廃止した。この資料の値・日付・不足は再計算しない。

帯は宣言予測母集団の観測分布で、未評価≠0損益。宣言/観測の月数・企業数・重みを分け、8.5%帯を[L3要求率](../doctrine.md#required-return-policy)へ連動させない。price-only/FY配当込みの母集団差を配当効果とせず、FY配当を支払/権利落ちcash ledgerとしない。

固定v43は粗い順位の研究材料に留める。退出・配当・PIT・実行価格の制約から数値較正/買い閾値へ[不採用](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-23-v43-evidence-decision/report.md)で、formal evidence不足/負の結果を効果・採用・利益の認定にしない。

現行の比較は[判断時点の選定・進捗と正式提案](screening-runtime.md#case-evidence)を読み、旧較正を更新しない。
