# 市場環境

経済原本のReadingと、市場・業種・個社の相対状態を扱うL2。取得は[source](data-sources.md)、財務は[企業財務](company-financials.md)、価格位置は[テクニカル分析](technical-analysis.md)、仮定付き価値は[銘柄評価](stock-assessment.md)、環境判断はInvestment Judgmentが所有する。

<a id="market-context-reads"></a>
## 比較する母集団と時点

各時点以下の最新masterの普通株を使い、除外銘柄を旧snapshotから戻さず、参照市場/業種を調査候補へ縮めない。[入力固定](../../engine/src/baibai_engine/machine_analysis/generation/runners/market_snapshot.py)と[算式・窓](../../engine/src/baibai_engine/machine_analysis/models/market_context/)を正本とする。

市場breadthはbenchmarkと同じ観測日の価格から作る。業種表はbenchmarkなしでも最新価格日で成立し得るが、市場breadthの成立を意味しない。業種中央値は各社のbar窓で、開始日まで揃うとは限らない。一方、個社相対はbenchmarkと自己除外peerに両端日を揃え、欠測を補間・業種中央値で代用しない。ratio差0.1は10 percentage pointsで、割安認定ではない。価格自己順位と旧倍率レンジ内位置も別の量である。

個社bar窓とTAの[共通calendar](technical-analysis.md#共通calendarの価格context)を分け、`price_path_relative`はTAの実在価格対と同端点で比較する。配当なしの調整終値returnとpeakからの下落を混ぜず、benchmark欠測は相対比較へ局所化する。旧runの候補母集団によるrelative strengthを現行値へ再計算しない。

調査はSelectionの固定generationを使い、決算予定・規制や現在の[台帳](portfolio-ledger.md)が含まれるとは扱わない。

<a id="margin"></a>
## 信用需給

全銘柄残高と選別subset、対象日と公表日の違いは[source境界](data-sources.md#信用残のsourceと日付)に従う。

[reader](../../engine/src/baibai_engine/machine_analysis/observations/trading_activity/margin_reader.py)は明示PubDateを優先し、なければ週次2/日次1営業日lagを使う。公表日当日は含めず、この暦規約を実配信時刻の証明にしない。

株数/出来高はExRTで確定した分割/併合だけで判定日の単位へ揃える。rights issueの価格factorを株数へ使わず、換算不能は影響する比率/差だけへ局所化する。同一残高のlong/short構成比まで消さず、残高/平均出来高を実際の解消日数予測としない。

<a id="reading"></a>
## Reading

[生成reader](../../engine/src/baibai_engine/machine_analysis/generation/runners/market_reading.py)が固定した保存観測とdated規則から水準・分布・鮮度を記述する。regime・合成score・売買signalにせず、L2系列を原本storeへ複製しない。読取は`baibai-engine market-context --help`へ。

統計の水準/前年比は`statistic`、比較条件は窓・観測数・samplingで確認する。latest/trendは元系列で、履歴不足・stale・取得失敗・分布の端を分ける。`--latest`は保存された最終観測で今日の値や鮮度を保証せず、次回公表の目安もcadence/lag推定で公式calendarではない。

Yahooの日足closeは途中値を含む。保存session/受領時計で判定し、現在時計から過去を推定しない。session終了・過去barも公式終値/先物清算値の保証ではない。ネット流動性はWALCL水曜残高・WTREGEN週平均・日次RRPを合わせる近似で、同時点残高や投資可能資金ではない。月次代表日・採用vintageを実公表時刻としない。

<a id="option-iv"></a>
### オプションIVの読み方

iv30/skewの限月選択・30日補間は[計算規約](../../engine/src/baibai_engine/machine_analysis/observations/economic_indicators/option_iv.py)。SQ直後/急変時の欠測を平穏、term負値をパニック、skew縮小を警戒後退、put/call差をsource誤りと断定しない。[過去の標本・診断](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-07-31-option-iv-quantiles/report.md)は売買閾値や収益の検証ではない。

### 再計算とPITの限界

`read_semantics.mode=recomputed_from_current_store`は後日取得・改定込みの再計算で、Researchの`known_at`は適用しない。公表vintageを持つsourceだけ対象日で制限し、bulk受領時計で一律に絞って当時既知の履歴を消さない。

固定snapshotは再開時の同一入力を保つが、intraday PITを保証しない。同じas-ofだけでは入力も固定されず、PubDate等は正確なAPI利用時刻・完全な改定履歴の証明ではない。当時判断・成績との比較にもこの限界を残す。

<a id="assessment"></a>
## 市場環境判断

Market Assessmentは固定入力/基準時点、重要な水準・変化、反証/不確実性を個社の耐性・仮定・支払価格へつなぐ。章数・確率格子・scenario件数を課さず、不足や小さな変化も扱う。独自risk点・指数連動要求率・自動配分・一律業種除外/売却は増分価値と費用・誤除外を確認できず未採用。[方式比較](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-20-macro-risk-strategy/report.md)は集約指数一般の無効や現在の市場状態を認定しない。

as_ofは対象日、known_atは入力確定時刻。対象日以下かつcutoffまでに実発行された判断を選び、週末発行も対象日末cutoffで落とさない。available_atへ元提供者の公表時刻を捏造せず、新判断は実発行後のSelectionで固定する。

作成・独立検証・CAS発行は[市場判断skill](../../.agents/skills/market-context-assessment/SKILL.md)へ。日次L1/L2から自動起動せず、L2値の第二正本を作らない。

## 保存履歴

exact ID/historyは保存本文・入力・引用・supersedesを検証して返し、新writer・現在cutoff・共通projectionで旧構造を書き換えない。保存判断の抽出を新判断の必須構造にしない。

分析用latestは対象日/実発行時刻で選んだ一件を検証し、不整合を旧正常値へのfallbackで隠さない。L2原本確認やlatest読取に未選択のL3全履歴検査を先行させない。旧runner inputがなければ保存本文から得た範囲を示し、当時の全入力を再現したとしない。
