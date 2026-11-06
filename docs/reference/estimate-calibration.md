# 見積り較正

旧ER/FV・四Approachを保存入力から過去as-ofへ再構成し、forward outcomeと照合する診断。資産全体の成績・L3評価とは別で、現行の経済経路政策は較正済みと扱わず、新Selectionには旧分布でなく`not_applicable`を返す。観測窓・計算・欠測判定は[較正owner](../../engine/src/baibai_engine/machine_analysis/historical_evaluation/)、実行は[OPERATIONS](../../jobs/OPERATIONS.md#定期maintenance)を参照する。

## 現行判断を将来比較へつなぐ前の診断

完了した一回のOperationから、保存済みの母集団・候補・選定・提案を[読取専用tool](../../tools/experiments/inspect_decision_cohort.py)で照合できる。既存のSelection・評価pair・Proposalを参照し、現在のgenerationや価格で埋めず、第二の判断正本を作らない。

```bash
uv run python -m tools.experiments.inspect_decision_cohort \
  --db <PRIVATE_APPLICATION_COPY> --operation-id <COMPLETED_OPERATION_ID> \
  --out <NEW_PRIVATE_JSON>
```

未選定は経済的見送りではなく、未完了は完成した`no_action`ではない。0対象でも当時の候補を残し、提案なしを捏造しない。行は候補と調査対象のunionであり、全母集団の詳細ではない。モデルrevision一覧も保存Selectionのsliceに限る。未完了・中断など他のOperationは数えないため、これだけで全開始件数を分母にした方法比較はできない。出力には私有の判断内容が入るため公開ログ・artifactへ転送しない。CLIの既知の失敗は例外classだけを表示し、検証エラー内の私有値やpathを出さない。

これは将来追跡の入力確認であり、成果・指値の因果効果・当時PIT・事前登録を証明しない。後日選んだ旧Operationのhashが一致しても事前固定した比較群とは呼べない。正式な比較では対象の選び方・観測窓・metricを結果を見る前に固定する。`baseline_available_at`はOperation完了時刻で、約定可能な時刻ではない。選定の比較は`screening_published_at`、最終提案の比較は`proposal_published_at`以後の観測を起点として分け、価格観測・取引可能時刻も確認する。後日の訂正・人間報告・取引・Retrospectiveは別時点の記録として照合し、当時の本文へ混入させない。

<a id="machine-valuation"></a>
## 保存済み機械ER/FVの意味

旧E[r]は年率の比較用prior、FVは機械比較anchorであり、個社価値・採否・保有期間を指定しない。3y/5yは実現値の比較窓で推計期間ではなく、[L3の条件付き価値](stock-assessment.md#scenario-arithmetic)と分ける。

carry欠測の0寄与は無配・株数不変の観測ではない。グロス株数減少も自己株取得cash・未消化枠・将来還元ではない。保存runの時点・basis・欠測を補って読み替えない。

### 旧倍率比較の読み方

- 業種中央値は旧Screening資格母集団の比較で、market fallbackを「同業より割安」と読まない。
- 自己レンジは現在fundamentalsを固定した価格比較で、当時の倍率履歴ではない。bar本数は暦の完全性を示さない。真の倍率履歴への置換は必要窓が成立せず[不採用](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-17-self-range-vs-multiple-history/report.md)。
- 3FY平均EPSは算術平均で、経済的な正常利益ではない。normalized PERのER/FV anchor追加は[不採用](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-02-normalized-per-production/report.md)。期間・basis・欠測年は[旧FY観測](../../engine/src/baibai_engine/machine_analysis/historical_evaluation/legacy_screening/annual.py)に従う。

<a id="carry-policy"></a>
### 機械priorの採否

[グロス株数→自己株控除後](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-17-tradable-share-change/report.md)への置換、[実現率引上げ](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-01-er-level-calibration/report.md)、[upside/buyback cap拡張](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-10-er-distribution-context-v2/report.md)は当時支持されず旧priorを維持した。将来の有効性の否定ではない。予想DPSの[spike guard](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-30-dividend-carry-spike-guard/report.md)は一回性分配の反復を抑える仮定で、盲検の閾値検証ではなく一部指標の悪化も受容した。

## 何を測るか

短期は回帰警報、1yは先行材料、3y/5yは方法変更の根拠。top-k/全推薦unionをoutcome前に固定し、欠測で下位を繰り上げない。月次gridは公式calendarや全取得完了の証明ではなく、実entryと名目horizonも区別する。重複cohort・等重みの単位を読み、月数を独立標本数としない。

## 入力と欠測

同日masterでも母集団の完全性は保証されず、prior/future snapshotや後日replayを当時母集団・PITと扱わない。脱落銘柄の不備は不明のままで、取得coverageは成功した要求範囲から読む。保存行や現provider契約から補わず、未観測の入力面を再現済みにしない。

未解決理由を区別し、row欠落を未上場等、未計測を「欠けなし」としない。退出不足/未評価銘柄の固定caseは必要metricの感度で、実現値の復元や真の上下界ではない。具体的な状態・case・metric別判定は較正ownerに従う。

## priceとtotal

相対ER較正は価格収束成分とprice-onlyの横断spreadを比較し、carry込みの絶対ER水準を検証しない。price/totalの母集団差を配当効果にしない。

FY配当込みは期間内に期末を迎えた年度のDivAnnを共通価格basisで測り、支払/権利落ちcash ledgerではない。観測0と欠測を分け、月割り・利回り×年数で補わない。不明を補わず、会計境界・配当basis・rights cashの判定は[forward測定](../../engine/src/baibai_engine/machine_analysis/historical_evaluation/forward.py)に従う。

## 退出価値の測定規約

廃止後の終値で閉じられない窓には成立した現金TOB価格を廃止日の代理値として使う（[導入比較](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-11-capital-control-exit-values/report.md)）。破綻型理由の退出は最終市場終値で測る。実約定・受領日・最終回収額・倒産・無価値の証明ではなく、長期停止の乖離も理由だけでは決まらない。案件・basisや退出対応が不明なら未解決のままにする。

<a id="er-level-distribution-context"></a>
## 旧判断へ渡す歴史分布

[published YAML](../../engine/src/baibai_engine/machine_analysis/historical_evaluation/context/published/er-level-calibration-latest.yaml)はGit reviewで同梱する単一artifactで、再生成snapshotとは別。未観測の母数/理由とformal evidence不成立を残し、破損・集合不整合をpartialにしない。

帯は宣言予測母集団で固定し、未評価を0損益にしない。宣言/観測の月数・企業数・重みを分け、8.5%帯を[L3要求率](../doctrine.md#required-return-policy)へ連動させない。観測分布は全母集団の期待収益・個別予測ではない。

[接続owner](../../engine/src/baibai_engine/investment_judgment/workflow/calibration_context.py)はidentity・有効期間が合う保存旧Selectionだけに添え、順位・要求率・保存入力を変えない。不適合なartifactへの付替えや日付/hashだけの更新はしない。固定v43は粗い順位の研究材料に留まり、退出・配当・PIT・実行価格の制約から数値較正/買い閾値へ[不採用](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-23-v43-evidence-decision/report.md)。採否は当時の構成に限り、手計算感度でformal gateを上書きしない。

## methodと再構築

旧較正は旧runの丸め済みStock Analysis・候補純関数を再生し、現行モデル/政策と混ぜない。revision・入力窓・forward規約が異なるcacheは再構築する。配置/import等だけで意味revisionを進めない。

`force`は指定範囲だけでcurrentを置換する。残す全cohortを含め、旧snapshotを先に消さず、通常dailyへ再構築を代行させない。公開/読取は[保存owner](../../engine/src/baibai_engine/machine_analysis/historical_evaluation/store.py)に従う。hydrateは取得coverageを復元しない。入力・取得記録・容量は[固定releaseの運用](../../jobs/OPERATIONS.md#local-release-hydrate)で確認する。

### pre-2019診断

必要履歴がない短窓variantは別snapshot/identityへ分離し、full-windowや標準手法の実証と呼ばない。

## 方法を変える判断

算術/期間/単位/basis修正と経済仮説の優劣を主張する変更を分ける。後者は仮説・対象・必要metric・採否を計測前にIssue/PRへ固定し、被覆・membership・集中、design/confirmの時間分割と既知結果への接触を示す。結果に合わせ条件を動かさない。

evidenceのcompleteは測定条件の充足で、効果・採用・利益の認定ではない。不足・負の結果・未決を分け、insufficientを効果なしにしない。採用は測定した構成に限り、個人の保有成果とreplayを合算しない。用途のない枝は[維持費と価値](../doctrine.md#development-investment-policy)から判断する。
