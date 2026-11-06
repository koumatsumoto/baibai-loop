# 保存済み旧評価の読取

旧ER/FV・四Approachの再推計・再選抜と較正の再生成・再配布は廃止した。保存済みrun・旧Selectionと同梱の凍結contextは読めるが、現行モデル・候補政策・L3評価・独立Reviewを代行しない。既存storeや原本は削除せず、当時の値・identity・時刻・欠測を保持する。

保存runはexact `run_revision_id` を指定した `historical-evaluation run-show` / `candidates show` で読む。旧Selectionは保存された分析を投影し、現在のrulesや新しい推計で埋めない。

## 現行判断を将来比較へつなぐ前の診断

完了した一回のOperationを[読取専用tool](../../tools/experiments/inspect_decision_cohort.py)で照合する。保存済みの母集団・候補・Selection・評価pair・Proposalを使い、現在値で補わず、第二の判断正本にしない。

```bash
uv run python -m tools.experiments.inspect_decision_cohort \
  --db <PRIVATE_APPLICATION_COPY> --operation-id <COMPLETED_OPERATION_ID> \
  --out <NEW_PRIVATE_JSON>
```

未選定を経済的見送り、未完を`no_action`としない。0対象でも当時の候補を残し、提案なしを捏造しない。行は候補と調査対象のunion、モデルrevisionは保存Selectionのsliceに限る。他の未完/中断Operationを数えず、全母集団の詳細や全開始件数を分母にした方法比較ではない。私有出力を公開ログ・artifactへ転送しない。

入力確認であって成果・指値の因果効果・当時PIT・事前登録を証明しない。旧Operationのhash一致も事前固定の証明でなく、正式比較の対象・窓・metricは結果を見る前に固定する。`baseline_available_at`は完了時計で約定可能時刻ではない。選定は`screening_published_at`、最終提案は`proposal_published_at`以後の観測から別に比べ、価格観測・取引可能時刻も確認する。後日の訂正・人間報告・取引・Retrospectiveを当時の本文へ混ぜない。

<a id="machine-valuation"></a>
## 保存済み機械ER/FVの意味

旧E[r]は年率の比較用prior、FVは機械比較anchorであり、個社価値・採否・保有期間を指定しない。3y/5yは実現値の比較窓で推計期間ではなく、[L3の条件付き価値](stock-assessment.md#scenario-arithmetic)と分ける。

<a id="carry-policy"></a>
carry欠測の0寄与は無配・株数不変の観測ではない。グロス株数減少も自己株取得cash・未消化枠・将来還元ではない。保存runの時点・basis・欠測を補って読み替えない。業種中央値のmarket fallbackを同業比較とせず、現在fundamentalsを固定した自己価格レンジを当時の倍率履歴としない。3FY平均EPSは算術平均で、経済的な正常利益ではない。

<a id="er-level-distribution-context"></a>
## 凍結した歴史分布

[同梱YAML](../../engine/src/baibai_engine/machine_analysis/historical_evaluation/context/published/er-level-calibration-latest.yaml)は当時の記録として保持する。日付/hashの更新や再生成で有効に見せない。[接続owner](../../engine/src/baibai_engine/investment_judgment/workflow/calibration_context.py)はidentity・有効期間が合う保存旧Selectionだけに添え、期限切れ・不一致・不足をそのまま示す。新GeneratedResearchSelectionには `not_applicable` を返す。

帯は宣言予測母集団の観測分布で、未評価は0損益ではない。宣言/観測の月数・企業数・重みを分け、8.5%帯を[L3要求率](../doctrine.md#required-return-policy)へ連動させない。price-onlyとFY配当込みの母集団差を配当効果とせず、FY配当込みを支払/権利落ちcash ledgerとしない。

固定v43は粗い順位の研究材料に留まり、退出・配当・PIT・実行価格の制約から数値較正/買い閾値へ[不採用](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-23-v43-evidence-decision/report.md)。formal evidenceの不足や負の結果を、効果・採用・利益の認定に読み替えない。
