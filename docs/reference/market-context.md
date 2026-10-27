# 市場環境

保存された経済原本のReadingと、市場・業種・個別株の相対状態を扱うL2領域。企業価値は[企業価値](company-valuation.md)、価格位置は[テクニカル分析](technical-analysis.md)、仮定を伴う環境判断はInvestment Judgmentが所有する。取得・原本はData Collection、現在のL2定義はindicators、計算・統計はreadingに置く。

<a id="market-context-reads"></a>
## 比較する母集団と時点

| 出力 | 比較対象 |
| --- | --- |
| 旧Stock Analysisの業種relative strength | 旧Screeningが渡す母集団で業種平均と市場平均を比較。個別対業種の差ではない |
| market snapshot | benchmarkと同じ観測日の普通株breadth・業種中央値。過去の各時点以下のmasterを使う |
| security context | 指定銘柄と同じ開始・終了日のbenchmark、自己を除いた同業peer。端点不足を補間しない |

masterはcutoff以下の一つの最新snapshotを選び、その中にないtickerを古いsnapshotから復活させない。普通株とETF等を混ぜず、価格・benchmark・master・要求日の各基準日を示す。peer中央値、市場の業種中央値、旧Screeningの業種平均は別母集団である。

価格は[調整済み価格reader](../../engine/src/baibai_engine/market/adjusted_prices.py)からprice-only basisで読む。個別対peerの差はratioの差で、0.1は10 percentage points。乖離を企業の割安認定へ変換しない。価格の自己順位百分位と企業価値モデルのmin/max内位置も別の量である。

既存security contextの`price.return_1d/5d/20d/60d`は個社観測bar差分、`high_52w`は直近252終値の高値で、共通営業日窓へ変更していない。新生成の`price_path_relative`だけがTAの[共通calendar観測](technical-analysis.md#共通calendarの価格context)と同じref・正確な開始/終了日でbenchmarkを比較する。G252は253営業日期間内の観測peak→現在で、旧252終値highとも前年比とも異なる。生成時に既読の長期benchmark系列と日状態を再利用し、端点が欠測・不整合なら当該比較だけを未評価にする。旧MC相対値・peer値の異窓流用や、新しいpeer比較は行わない。

旧ticker-profileは旧runと可変モデル・現在ledgerを混合するため撤去した。調査のモデル入力はselection-contextで固定generationから読み、取引事実はPosition、決算予定・規制等の原観測は各market readerから必要時に確認する。selection-contextへイベント一覧や現在ledgerの集中度が含まれるとは扱わない。五モデルの共通生成は全masterと共有価格から市場・業種の参照文脈を作り、個社だけを調査するときも参照母集団を候補集合へ縮めない。

<a id="margin"></a>
## 信用需給

[sourceの母集団と公表日](data-sources.md#信用残のsourceと日付)を先に確認する。全銘柄旧週次と全銘柄日次は残高対象日で使い分け、日々公表subsetを全銘柄の欠損補完やゼロへ変換しない。

levelは公表済み直近残高で、cadenceが変わっても語義は変わらない。26wは**26 ISO週前との比較**で、日次側も各ISO週の最終残高日をsamplingする。26行前ではなく、欠測週を補間せず、固定半年差とも扱わない。

残高日、PubDate、利用可能cutoffを区別する。週次は第2営業日、日次は翌営業日の公表を待ち、L2読取は公表日当日を含めない。この暦規約は実際のAPI配信時刻の証明ではない。

残高・出来高の株式単位を判定日へ揃える時はExRTで分割/併合と確定した係数だけを使い、rights issueの価格factorを株数へ適用しない。換算不能が影響する比率・差だけを未評価にし、同一残高のlong/short構成比まで消さない。残高/平均出来高は規模比較で、実際の解消日数予測ではない。

<a id="reading"></a>
## Reading

Readingは保存L1とdated規則から記述統計・鮮度を計算する。regime、合成score、売買signalではなく、系列ごとに水準が歴史分布のどこにあるかを読む共通の物差しである。L2系列・scalar履歴を原本storeへ複製しない。

```bash
uv run baibai-engine market-context reading --asof <YYYY-MM-DD> --format json
uv run baibai-engine market-context snapshot --asof <YYYY-MM-DD> --format json
uv run baibai-engine market-context indicators get jp.policy_rate --latest
```

指標CLIの`--latest`は保存入力から得られる最後の観測を元の日付で返す。今日の値や鮮度の保証ではなく、当日rangeの欠測を補完しない。

percentile/z-scoreの対象がlevelかyoyかをstatisticで確認する。latestやtrendまでyoyへ変わるわけではない。実効窓・観測数・sampling頻度を揃え、疎な系列を密な日次履歴と同列に比較しない。履歴不足でも最新値そのものが誤りとは限らず、stale・取得失敗・分布の端を区別する。next_print_estimateはcadence/lagの推定で公式calendarではない。

ネット流動性は入力の単位を揃えるが、WALCLの水曜残高、WTREGENの週平均、日次RRPを組み合わせる近似であり、同時点の厳密な残高や投資可能資金ではない。月次整列の代表日や採用入力vintageも実公表時刻ではない。

Readingの規則は[所有ドメインのdated YAML](../../engine/src/baibai_engine/machine_analysis/market_context/reading/rules/)と出力のrules_revision（filename stem）で識別する。物理配置とrevisionを分け、保存判断が引用した数値・意味を配置整理で変えず、current copyを増やさない。式変更は保存原本の再読で行い、必要な原本不足がある場合だけ取得する。

<a id="option-iv"></a>
### オプションIVの読み方

iv30・skewは30日を挟む適格な限月を要し、skewの期間補間は逆平方根の満期軸を使う規約である。SQ直後や急変時の欠測を平穏とせず、termの負値だけをパニック、skewの縮小だけを警戒後退としない。put/callのIV差は両限月で確認するが、差だけでsourceの誤りを確定しない。[当時の標本と診断](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-07-31-option-iv-quantiles/report.md)は売買閾値や将来収益の検証ではない。

### 再計算とPITの限界

read_semanticsがrecomputed_from_current_storeなら現在storeの後日取得・改定情報から再計算している。Investment Judgmentのknown_atはReadingへ一律適用されず、ProviderSpecが公表vintage能力を持つと宣言したsourceだけを対象日でclampする。bulk履歴の受領時刻で一律clampすると、当時既知だった履歴まで消してしまう。

as-ofの固定やJST日末cutoffは**intraday PITの保証ではない**。公表日、受領時刻、改定vintageの能力を確認し、当時の判断品質や成績との照合へこの限界を残す。投資部門別PubDateも改定の正確なAPI利用可能時刻ではない。snapshot固定は再開時の同一入力を保つ。

<a id="assessment"></a>
## 市場環境判断

Investment Judgmentの市場環境判断（Market Context Assessment）は、固定入力と基準時点、重要な水準・変化の根拠、反証/不確実性、調査・業種・riskへの含意を扱う。章数・確率格子・scenario件数を必須にせず、変化が小さいことや不足も表現する。単なる株価トレンドへ縮めず、個社への伝達と支払価格の比較につなぐ。

共通条件を個社の耐性・仮定と価格比較へ接続する。独自の総合risk点、指数連動の要求率、自動配分・一律業種除外/売却は、実時間の増分価値と費用・誤除外を確認できず採用していない。[当時の方式比較](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-20-macro-risk-strategy/report.md)は、集約指数一般の無効や今日の市場状態を認定したものではない。

as_ofは判断対象日、known_atは入力確定時刻。対象日以下でcutoffまでに発行された判断を実時刻で選ぶ。週末発行の前営業日評価も扱い、対象日末を一律のcutoffにしない。available_atを記す場合も元提供者の公表時刻を捏造しない。新判断を開始入力に使うなら実発行後のSelectionで固定する。

作成・独立検証・CAS発行は[市場判断skill](../../.agents/skills/market-context-assessment/SKILL.md)へ進む。日次のL1/L2更新から自動起動せず、L2の値を別の数値正本へ保存しない。

## 保存履歴

exact IDによる閲覧は保存本文・入力・引用・supersedesをそのまま返し、新writerの構造や現在のcutoffで書き換えない。分析用の選択とは別である。古い章・scorecardを共通projectionだけで置き換えず、保存判断の抽出を新しい判断の必須構造にしない。

L2原本確認のためにL3全履歴を先行validationする手順は持たない。分析用latestは対象日・公表時刻で選んだ一件のpayload・identityを確認し、未選択の全履歴本文を前提にしない。選択本文の不整合は古い正常な判断へのfallbackで隠さない。exact ID/historyは明示した保存履歴を検証して読む。旧runner inputがなければ保存本文から得る範囲を示し、当時の全入力を再現したとは扱わない。
