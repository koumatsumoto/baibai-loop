# 見積り較正

旧ER/FV・四Approachを保存入力から過去as-ofで再構成し、forward outcomeと照合する横断診断。資産全体の運用成績やL3銘柄評価とは別で、現行の経済経路政策を較正済みとは扱わない。新しいSelectionには旧ER分布を付けず、`not_applicable`を返す。計算・field・検証条件は[較正処理](../../engine/src/baibai_engine/machine_analysis/historical_evaluation/)、実行は[OPERATIONS](../../jobs/OPERATIONS.md#定期maintenance)が所有する。

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

旧機械E[r]は倍率収束とcarryによる年率の比較用prior。企業固有の価値や採否、保有期間を指定するものではない。較正の3y/5yは実現値を比べる窓であり、推計自体の期間と混同しない。企業の条件付き算術は[Stock Assessment](stock-assessment.md#scenario-arithmetic)が所有する。

carryの配当・グロス株数変化が欠測なら計算上の寄与は0になるが、無配・株数不変を観測した意味ではない。元のnullable入力と仮定を合わせて読む。株数減少は消却・発行等を含む過去の変化で、自己株取得の実行cash・未消化枠・将来還元ではない。

業種中央値・自己レンジanchorは機械比較値で、企業の期間内分配・将来終価・現在価値とは別である。保存runの値を時点・basisとともに読み、旧snapshotの不在field・null・0を補って再発行しない。論理as-ofで選んだrunにも後日replayがあり、当時既知の保証にはならない。

### 旧倍率比較の読み方

業種中央値は当時のScreening資格母集団による比較。market fallbackを「同業より割安」と読まず、現行モデルの市場・同業文脈と分ける。母数下限も経済的最適値ではない。

自己レンジは現在fundamentalsを固定し、過去価格だけを動かした比較である。当時の利益・資本による倍率履歴でも、min/max内の位置による観測percentileでもない。bar本数や密度はcalendar完全性を証明しない。真の倍率履歴への置換は必要窓が成立せず[不採用](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-17-self-range-vs-multiple-history/report.md)とした。

3FY平均EPSのnormalized PERは算術平均で、循環・一時損益を経済的に正常化した利益ではない。旧候補抽出に使ったがER/FV anchorへの追加は[採用していない](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-02-normalized-per-production/report.md)。期間・株式basis・欠測年の扱いは[旧FY観測](../../engine/src/baibai_engine/machine_analysis/historical_evaluation/legacy_screening/annual.py)から辿る。

<a id="carry-policy"></a>
### 機械priorの採否

グロス株数変化から自己株控除後への置換、実現率引上げ、upside/buyback cap拡張は当時の比較で支持されず、既存priorを維持した。詳細は[株数比較](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-17-tradable-share-change/report.md)、[実現率](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-01-er-level-calibration/report.md)、[cap比較](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-10-er-distribution-context-v2/report.md)に残す。将来の有効性を否定する判定ではない。

予想DPSのspike guardは一回性分配の反復を抑える仮定で、盲検の閾値検証ではなく一部指標の悪化も受容した。[採用時の制約](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-30-dividend-carry-spike-guard/report.md)を将来成績の保証にしない。

## 何を測るか

短期は回帰警報、1yは先行材料、3y/5yは手法変更の根拠として区別する。通常診断と事前に対象・必要metricを定めた方法比較は別であり、evidenceのcompleteは測定条件の充足であって効果の正しさや採用の認定ではない。

推薦集合を先に固定し、top-kと全推薦のunionを混同せず、outcomeの欠測で下位を繰り上げない。月次gridは条件を満たす月内最終観測日で、公式月末営業日や全銘柄取得完了を保証しない。entry許容窓により実保有期間が名目horizonより長くなる場合もある。重複cohortや等重みの単位を区別し、月数を独立標本数として有意性を主張しない。

## 入力と欠測

同日masterと当時の母集団を区別する。prior/future snapshotは診断で使えても正式な過去母集団にはならず、既に落ちた銘柄の不備は観測できない。取得coverageは成功した要求範囲で、行のmin/max・manifest件数・現在のprovider契約から作らない。古いcohortの短い履歴や未観測EDINET軸を、全入力面の再現と扱わない。

未価格entry、panelとのentry gap、未成熟、退出不足は別の未解決理由である。row不在やclose/volume nullだけで未上場・停止・無約定と決めず、未計測を「欠けなし」にしない。

退出不足・priced-master未評価の感度は、対象を同定した全損・中立の固定caseで結論の安定性を調べるもの。実現値の復元や真の上下界ではない。必要metric自身への影響と共有入力の不備を区別し、件数や目的外metricの反転だけで一律判定しない。

## priceとtotal

相対ER較正は価格収束成分とprice-only実現値の横断spreadを比較する。carryは同じbasisで観測されないため、絶対ER水準の検証にはならない。price/totalで評価可能母集団が異なれば中央値の差を配当効果と解釈しない。

FY実績配当込みは、期間内に期末を迎えた年度のDivAnnを共通価格basisへ揃えた測定である。支払日・権利落ち日のcash ledgerではなく、端のFYの月割りやentry利回り×年数の代用もしない。配当0は観測、欠損・期間gap・basis不明は未解決。rights issueの売却/払込cashは価格factorだけでは復元できない。

配当期間の欠落は既知の会計境界で確認し、後続資料がないだけで12か月決算を仮定しない。exit後の資料を境界確認に使っても、その配当額を加算しない。細部は[forward測定](../../engine/src/baibai_engine/machine_analysis/historical_evaluation/forward.py)が所有する。

## 退出価値の測定規約

成立した現金TOB価格を廃止後の終値がない窓の退出代理値に使う。廃止日にその値で退出した扱いは測定規約で、実約定・受領日・非応募者の最終対価の証明ではない。JPXの買収等の理由だけで現金対価を認定せず、案件・成立・株式basisが未確定なら未解決とする。[導入比較](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-08-11-capital-control-exit-values/report.md)を参照する。

破綻型理由の退出は最終市場終値で測る。倒産・無価値・最終回収額の認定ではなく、長期停止による乖離も理由だけでは確定できない。廃止後取引や複数退出など、対応が曖昧な窓を推定で解消しない。

<a id="er-level-distribution-context"></a>
## 旧判断へ渡す歴史分布

配布する[published YAML](../../engine/src/baibai_engine/machine_analysis/historical_evaluation/context/published/er-level-calibration-latest.yaml)はGitでreview・同梱する単一artifactで、再生成用snapshotとは別物である。結果観測不能の母数と理由を残し、観測分布とformal evidence不成立を分ける。破損・集合不整合・method不一致をpartialとして許容しない。

帯は宣言した予測母集団から固定する。未評価を0損益へ入れず、宣言/観測の月数・企業数と重みを区別する。固定8.5%帯は比較座標で、[L3の要求率](../doctrine.md#required-return-policy)へ連動させない。分布は観測済みの歴史であり、全母集団の実証済期待収益や個別予測ではない。

対応帯を読めるのはrules/model identityと有効期間が適合する保存旧Selectionだけで、現行の生成モデルへは適用しない。[判断への接続](../../engine/src/baibai_engine/investment_judgment/workflow/calibration_context.py)は候補順位・要求率・保存入力を変えない。旧runへ不適合なartifactを付け替えず、YAMLの日付・hashだけを更新しない。

固定v43標本には粗い順位の研究材料があるが、退出対価・配当basis・過去入力/PIT・実行可能価格の制約が残るため、期待リターンの数値較正や買い閾値には採用しない。[当時の採否](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-23-v43-evidence-decision/report.md)を後日の入力・方法へ読み替えず、手計算感度でformal gateを上書きしない。

## methodと再構築

旧較正は旧runと同じ丸め済みStock Analysis・候補純関数を使い、現行モデル・候補政策とは契約を分ける。異なるrevision・入力窓・forward規約のcacheを流用せず、必要時に再構築する。文書・配置・importだけで意味revisionを進めない。

calibrationは再生成可能なローカルsnapshot。`force`は指定範囲だけでcurrentを置換するため、残す全cohortを含め、旧snapshotを先に消さない。通常dailyに再構築を代行させない。公開と読取の仕組みは[保存owner](../../engine/src/baibai_engine/machine_analysis/historical_evaluation/store.py)、操作は[運用手順](../../jobs/OPERATIONS.md#定期maintenance)に従う。

固定lake releaseのhydrateはstore-local取得coverageを復元しない。再実行だけでcoverage不足が埋まると考えず、実入力と取得記録を照合する。行数から取得記録を作ったり、隔離コピーの容量guardを緩めたりしない。

### pre-2019診断

標準の必要履歴がない区間の短窓variantは、別の作業snapshotとidentityに分離する。短い履歴をfull-windowや標準手法の実証と呼ばない。

## 方法を変える判断

算術・期間・単位・basisの修正と、経済仮説の優劣を主張する変更を分ける。後者は計測前に仮説・対象・必要metric・採否基準をIssue/PRへ固定し、被覆・membership・集中を先に確認する。結果を見て条件を動かさず、design/confirmの時間分割と既知結果への接触を示す。

窓ごとの不足・負の結果・未決を区別し、insufficientを効果なしとしない。用途のない評価枝を成熟待ちだけで残さず、[開発原則](../doctrine.md#development-investment-policy)から維持費と価値を判断する。採用は計測した構成と一致させ、個人の保有成果と機械replayを合算しない。記述分布の生成成功を、方法の優位や運用利益の証明として掲げない。
