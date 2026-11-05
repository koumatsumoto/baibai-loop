# 市場環境

保存された経済原本のReadingと、市場・業種・個別株の相対状態を扱うL2領域。財務・利益価格の観測は[企業財務](company-financials.md)、仮定付き企業価値評価は[銘柄評価](stock-assessment.md)、価格位置は[テクニカル分析](technical-analysis.md)、仮定を伴う環境判断はInvestment Judgmentが所有する。取得・原本、共通の経済指標観測、市場モデルの計算・統計を別の責務として扱う。

<a id="market-context-reads"></a>
## 比較する母集団と時点

普通株の母集団は各時点以下の最新masterを使い、除外された銘柄を古いsnapshotから戻さない。全市場・業種の参照母集団を調査候補だけへ縮めない。入力固定は[生成owner](../../engine/src/baibai_engine/machine_analysis/generation/runners/market_snapshot.py)、算式・窓・fieldは[市場モデル](../../engine/src/baibai_engine/machine_analysis/models/market_context/)が所有する。

市場snapshotのbreadthはbenchmarkと同じ観測日の普通株を使う。業種表はbenchmarkがなくても保持価格の最新観測日で成立し得るが、benchmark比較や市場breadthの成立を意味しない。業種の騰落率中央値は各銘柄の観測bar窓を集計するため、開始日まで揃った比較とは限らない。

個別株の相対値は、その銘柄と同じ開始・終了日のbenchmarkと、自己を除く同業peerを比較する。端点不足を補間せず、業種表の中央値で代用しない。差はratioの差で、0.1は10 percentage points。価格は配当を含まない調整済み終値であり、乖離を企業の割安認定にしない。価格の自己順位百分位も、旧較正の倍率レンジ内位置とは別の量である。

個社の観測bar窓と、TAの[共通calendar観測](technical-analysis.md#共通calendarの価格context)は別の時間軸である。`price_path_relative`はTAの実在する価格対と同じ端点を使い、benchmark不足を相対比較だけへ局所化する。観測peakからの下落を前年比returnや旧bar窓の高値差へ読み替えない。旧runの業種relative strengthも当時の候補母集団による別定義なので、現行値へ再計算しない。

調査はSelectionの固定generationを使う。決算予定・規制等の原観測と現在の[台帳](portfolio-ledger.md)は別に確認し、固定モデル入力に含まれるとは扱わない。

<a id="margin"></a>
## 信用需給

[sourceの母集団と公表日](data-sources.md#信用残のsourceと日付)を先に確認する。全銘柄旧週次と全銘柄日次は残高対象日で使い分け、日々公表subsetを全銘柄の欠損補完やゼロへ変換しない。

levelは公表済み直近残高で、cadenceが変わっても語義は変わらない。26wは**26 ISO週前との比較**で、日次側も各ISO週の最終残高日をsamplingする。26行前ではなく、欠測週を補間せず、固定半年差とも扱わない。

残高日、PubDate、利用可能cutoffを区別する。週次は第2営業日、日次は翌営業日の公表を待ち、L2読取は公表日当日を含めない。この暦規約は実際のAPI配信時刻の証明ではない。

残高・出来高の株式単位を判定日へ揃える時はExRTで分割/併合と確定した係数だけを使い、rights issueの価格factorを株数へ適用しない。換算不能が影響する比率・差だけを未評価にし、同一残高のlong/short構成比まで消さない。残高/平均出来高は規模比較で、実際の解消日数予測ではない。

<a id="reading"></a>
## Reading

Readingは保存観測と[dated規則](../../engine/src/baibai_engine/machine_analysis/models/market_context/reading/rules/)から系列の水準・歴史分布・鮮度を記述する。regime、合成score、売買signalではなく、L2系列を原本storeへ複製しない。計算は[Readingモデル](../../engine/src/baibai_engine/machine_analysis/models/market_context/reading/)、保存原本からの入力選択と読取制約は[生成reader](../../engine/src/baibai_engine/machine_analysis/generation/runners/market_reading.py)を辿る。読取commandは`baibai-engine market-context --help`を参照する。

percentile/z-scoreが水準と前年比のどちらを比較するかは`statistic`で確認する。latest・trendは元系列のままで、統計窓・観測数・sampling頻度を揃えて読む。履歴不足でも最新値が誤りとは限らず、stale・取得失敗・分布の端を区別する。指標CLIの`--latest`も保存入力の最後の観測を元の日付で返すだけで、今日の値や鮮度を保証しない。次回公表の目安はcadence/lagの推定で、公式calendarではない。

Yahooの1日足closeは取引途中の値を含む。保存session・受領時刻で途中と確認できた意味をReadingへ渡し、現在時刻から過去の受領を判定しない。session終了や過去日のbarも公式終値・先物清算値の確定証明ではない。

ネット流動性はWALCLの水曜残高、WTREGENの週平均、日次RRPを単位を揃えて組み合わせる近似で、同時点の厳密な残高や投資可能資金ではない。月次の代表日や採用vintageも実公表時刻ではない。

<a id="option-iv"></a>
### オプションIVの読み方

iv30・skewは適格な30日限月をそのまま使うか、30日を挟む2限月から補間する。[補間と基準検査](../../engine/src/baibai_engine/machine_analysis/observations/economic_indicators/option_iv.py)は計算規約であり、SQ直後や急変時の欠測を平穏としない。termの負値だけをパニック、skewの縮小だけを警戒後退とせず、put/callのIV差だけでsource誤りを確定しない。[当時の標本と診断](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-07-31-option-iv-quantiles/report.md)は売買閾値や将来収益の検証ではない。

### 再計算とPITの限界

`read_semantics.mode`が`recomputed_from_current_store`なら、現在の保存履歴から後日取得・改定値を含めて再計算している。ReadingへResearchの`known_at`は適用されず、公表vintageを持つsourceだけを対象日で制限する。bulk履歴の受領時刻で一律に絞ると、当時既知だった履歴まで消してしまう。

snapshot固定は再開時の同一入力を保つが、**intraday PITの保証ではない**。同じas-ofだけでは後日再計算の入力も固定されない。投資部門別PubDateを含め、公表日は正確なAPI利用可能時刻や改定履歴の完全性を示さない。当時の判断・成績と照合する際もこの限界を残す。

<a id="assessment"></a>
## 市場環境判断

Investment Judgmentの市場環境判断（Market Assessment）は、固定入力と基準時点、重要な水準・変化の根拠、反証/不確実性、調査・業種・riskへの含意を扱う。章数・確率格子・scenario件数を必須にせず、変化が小さいことや不足も表現する。単なる株価トレンドへ縮めず、個社への伝達と支払価格の比較につなぐ。

共通条件を個社の耐性・仮定と価格比較へ接続する。独自の総合risk点、指数連動の要求率、自動配分・一律業種除外/売却は、実時間の増分価値と費用・誤除外を確認できず採用していない。[当時の方式比較](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-20-macro-risk-strategy/report.md)は、集約指数一般の無効や今日の市場状態を認定したものではない。

as_ofは判断対象日、known_atは入力確定時刻。対象日以下でcutoffまでに発行された判断を実時刻で選ぶ。週末発行の前営業日評価も扱い、対象日末を一律のcutoffにしない。available_atを記す場合も元提供者の公表時刻を捏造しない。新判断を開始入力に使うなら実発行後のSelectionで固定する。

作成・独立検証・CAS発行は[市場判断skill](../../.agents/skills/market-context-assessment/SKILL.md)へ進む。日次のL1/L2更新から自動起動せず、L2の値を別の数値正本へ保存しない。

## 保存履歴

exact IDによる閲覧は保存本文・入力・引用・supersedesをそのまま返し、新writerの構造や現在のcutoffで書き換えない。分析用の選択とは別である。古い章・scorecardを共通projectionだけで置き換えず、保存判断の抽出を新しい判断の必須構造にしない。

L2原本確認のためにL3全履歴を先行validationする手順は持たない。分析用latestは対象日・公表時刻で選んだ一件のpayload・identityを確認し、未選択の全履歴本文を前提にしない。選択本文の不整合は古い正常な判断へのfallbackで隠さない。exact ID/historyは明示した保存履歴を検証して読む。旧runner inputがなければ保存本文から得る範囲を示し、当時の全入力を再現したとは扱わない。
