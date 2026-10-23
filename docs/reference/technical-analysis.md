---
title: "Technical Analysis"
summary: "保存価格から読むSMA20/50/200、Bollinger Bands20、Wilder RSI14の計算・時点・Research入力。"
doc_type: reference
status: active
---

# Technical Analysis

Technical Analysisは、Valuation、Market Context、External Assessmentと区別して価格の位置・変動を読むL2である。保存済み価格だけを入力に、SMA20/50/200、Bollinger Bands20、Wilder RSI14を返す。企業価値、売買判断、候補順位、合成scoreは作らない。新しいsource・DB・経済Reading registryを持たず、L1へ派生指標を保存しない。

算術は[`market_context/technical.py`](../../engine/src/baibai_engine/market_context/technical.py)、保存読取は[`technical_query.py`](../../engine/src/baibai_engine/market_context/technical_query.py)、公開facadeは[`read_api/technical_analysis.py`](../../engine/src/baibai_engine/read_api/technical_analysis.py)が所有する。package配置は既存の価格算術と共有し、分析結果は独立した`technical_analysis`として渡す。

## 入力・時点と不足

既存の価格readerを`start=date.min, end=as_of`で呼び、指定銘柄の保存済み全履歴を日付昇順で一度読む。raw closeを、価格日より後・as-of以下の保存価格調整factorで同じbasisへ揃える。closeがnullのactionも調整から落とさない。現金配当込みの価格やreturnではない。

null closeは終値観測に数えず、補間・前日値補完をしない。bar数は観測数であり連続営業日・暦日数ではない。指標ごとの実効本数と開始・終了日を返す。`available`は計算可能という意味で、最終価格を要求日に付け替えず、鮮度・流動性・取引可能性を保証しない。不足指標だけをnullにし、他の指標や財務評価・Researchを一律停止しない。

観測日のcutoffは守るが、後日取得・訂正前の価格vintageの再現は保証しない。訂正・保存履歴の延長で計算値が変わり得る。出力の計算revisionと実効履歴を識別し、Researchは読んだ結果を固定する。

## 移動平均

SMAは当該終値を含む最後の20/50/200観測barの算術平均。各窓に必要な本数がなければ短縮計算しない。窓内の非正・非有限価格は、そのSMAを計算不能とする。200本不足でも20/50本線や他指標の値は保持する。

各線の`value`は価格単位、`gap_ratio`は`latest_close / SMA - 1`。割合を表示する場合だけ100倍する。短・中・長の3本とBB20中心線を共用する簡潔な設定であり、日本株の最適期間や予測優位の証拠ではない。現在の価格との上下、線の並び、乖離を読めるが、傾きや新たなクロス発生は検出しない。EMA、期間UI、売買閾値は持たない。

## Bollinger Bands20

当該barを含む直近20終値のSMAを中心に、価格の母標準偏差（ddof=0）の2倍を上下へ置く。終値が未丸めの上下バンドを厳密に超えた場合だけ`above_upper / below_lower`とし、接触は`within_bands`。現在の窓に対する状態であり、新規crossではない。

`sigma`は価格単位、`bandwidth = (upper_band - lower_band) / middle_band`は比率、`percent_b = (close - lower_band) / (upper_band - lower_band)`、`z_score = (close - middle_band) / sigma`である。percent_bを0〜1へclipしない。ゼロ分散は`zero_variance`、sigma/bandwidthは0、上下バンドは中心と同値、percent_b/z-score/positionはnullとする。20本不足や窓内の不正価格は理由付き計算不能とする。

リターンの実現volatilityや順位百分位とは別の量である。本番Market Contextの自己順位は最大750観測barで、短期上側超過と長期低位、短期下側超過と長期高位は両立する。20本の順位との包含には有限標本の算術上の制約があり、それだけで有用・無用を決めない。正規性、95%包含、反転確率は仮定しない。

## Wilder RSI14

保存された全有効日付prefixを使う。最初の15終値から14価格差を作り、正の差のgainと負の差の絶対値lossをそれぞれ14本で平均してseedする。以後の全観測差分について`average = (13 * previous_average + current_gain_or_loss) / 14`を再帰適用する。直近14差分の単純平均へ差し替えず、任意の計算開始日optionを持たない。15終値未満は不足、prefixに非正・非有限価格があればRSIは`invalid_price`。その古い不正値が有効な最新窓のBBやSMAまで無効にすることはない。

gain/lossがともに正なら`100 * average_gain / (average_gain + average_loss)`。gainだけ正なら100、lossだけ正なら0、両方0なら50とする。50は明示的なflat conventionで、確率ではない。上昇後の横ばいではgainが減衰してもloss=0を保つため、単に横ばいになっただけで50へ戻さない。履歴開始日、全bar数、seed終端日を併記し、保存履歴の長さによる違いを隠さない。

## 読取とResearchへの接続

```bash
uv run baibai-engine technical-analysis show --ticker 7203 --asof 2026-10-16
```

`--db`は既存market SQLiteを指定できる。取得・更新・注文は行わず、store未作成も計算不能指標を持つ読取結果として返し、DBを作らない。破損・旧schema・価格調整factor不正を正常な欠測へ隠さない。

ticker-profileは独立した`technical_analysis`を返す。新規Research固定入力の各銘柄は`market_context / external_assessment / technical_analysis`を保持し、Valuationと別の材料として投影する。Research内部snapshotの`securities_context[ticker].technical_analysis`は、人間が読む`domain-inputs.yaml`では`securities[ticker].technical_analysis`へ投影される。旧`price.bollinger_20bar`への恒久aliasは設けない。旧保存snapshotの2キーや当時のBB欄は書換えず、そのまま再開できる。未知の領域キーは引き続き拒否する。WebのMarket Context用DTOへ混ぜず、新しいWeb画面を本読取追加の前提にしない。

200日線付近の価格を成長・本源価値・大底と認定しない。価格だけが下がった場合と財務・事業価値が毀損した場合をValuation・Researchで別に確認する。BB下側や低RSIを買い、上側や高RSIを売りと自動解釈しない。トレンド継続、価格水準の変更、疎な取引も対照にする。

L2読取・Research固定入力への接続と、既存の評価・Review・提案欄へ所見をつなぐL3案内を実装した。[四領域の結合検証](../../reports/studies/2026-10-22-l3-four-domain-integration/report.md)では、相反・欠測を含む固定入力から評価・Review・Proposalへの保存と読戻し、後発入力からの独立性を確認した。採用経路はReviewed Assessment全文の参照とexact ID/hash/Reviewの束縛であり、SelectionをProposalへ直接再投影する案は保留している。これは有限ケースの接続検証であり、候補発見・調査優先度・予測効果の改善や、本番の買付提案完成を意味しない。採否と確認限界は同報告と[Issue #1567](https://github.com/koumatsumoto/baibai-loop/issues/1567)を参照する。
