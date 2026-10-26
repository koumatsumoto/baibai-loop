# テクニカル分析

保存価格から位置・勢い・変動幅を読む独立L2領域。`machine_analysis/technical_analysis/`が計算・query・CLIを所有し、[共有価格basis](../../engine/src/baibai_engine/market/price_basis.py)を使う。企業価値・候補順位・合成score・自動売買を作らず、新しいsourceやstoreを持たない。

## 入力と解釈

raw closeをas-of以下の価格調整factorで揃えたprice-only系列で、現金配当を含まない。close欠損日のactionも調整から落とさず、終値は補間・前日値補完しない。bar数は観測数であり連続営業日数ではない。計算可能は鮮度・流動性・取引可能性の保証ではなく、不足指標だけを未評価にする。

SMA20/50/200は現在価格との位置・並び・乖離を読む。期間は簡潔な慣例で、日本株での最適性を実証した値ではない。傾きや新規crossを推測しない。BB20の帯は価格分布の記述で、正規性、95%包含、反転確率は仮定しない。

Wilder RSI14は保存済み全履歴のseedと再帰に依存する。履歴の開始・本数・seed情報を確認し、直近14差分の単純平均と混ぜない。完全flatの50は計算上のconventionで、独立した中立判断ではない。履歴延長やprovider訂正で計算値は変わり得る。

12–1か月momentumは共通営業日のP(t−21)/P(t−252)−1で、直近月を除く価格変化。共通calendarの必要範囲に穴があれば欠測とし、個社の欠測端点を近隣価格へずらさない。SMA・RSI等の個社観測bar窓とは別の時間軸である。

## 共通calendarの価格context

新生成の`price_path`は、期待する最新営業日tを保存calendarから決め、R1/R20を正確なt−1/t−20とtの終値で計算する。内部の価格欠測だけで両端点のreturnを消さない。G60/G252はそれぞれ60/252営業日差分の期間（最大61/253日）内に**観測された最高終値**からtへの変化で、同値最高値のanchorは最新日。欠測本数だけ窓を過去へ延長しない。G252は前年比returnではないため、`window_start_price`と開始日→tの`window_return`を併記する。開始価格の不足はこのreturnだけを欠測にする。

as-of調整基準、期待価格日と実際の最新終値日、比較端点と価格、窓内の実効価格数・日状態と欠測日を保持する。固定snapshotのO/H/L/C/Vo/Vaの既存projectionを一括読取し、全6nullは当日約定なしとして数える。通常の無約定か終日停止かという理由は識別できない。[配信元仕様](https://jpx-jquants.com/ja/spec/eq-bars-daily)に対するcanonical列の意味で、native応答全件の再照合ではない。row absent、部分null、非有限・非正価格等の不正値を区別する。価格のない日のactionは独立した既存readerから保持する。

注記は`R1 <= -0.08 OR R20 <= -0.20 OR G60 <= -0.20`（B）で、研究対象の価格経路を読むための仮値である。最適閾値、投資採否、自動Research開始、追加調査義務ではない。G252は常時contextで、長期閾値Cは設けない。`calculation_revision`と`annotation.policy_revision`を分け、使用thresholdsを保存する。閾値ぴったりを含める比較は既存調整済みfloatの十進文字列による価格対のcross-productで行い、出力returnや観測価格は丸めない。調整時点で既に生じたfloat誤差を復元する契約ではない。

`matched_refs`は真、`evaluated_refs`は真偽を確定したref、`unavailable_refs`は不明で、対象はR1/R20/G60だけ。いずれか真ならmatched、全て偽ならnot_matched、それ以外はunassessableとなる。partialなGでも実在価格対で閾値到達を示せれば真とし、未達で未分類のabsenceが残れば不明とする。全日がpriced/no-tradeで説明できれば非発火を確定できる。ただし期待最新終値、必要calendar、過去比較価格の不足は不明で、古値・0に置き換えない。

TAは絶対価格経路・coverage・注記を所有する。生成時に小さな価格対だけをMCへ渡し、MCの`price_path_relative`が既読benchmarkの同端点比較を保持する。benchmark端点の不足はその比較だけを未評価にし、TAの陽性は消さない。旧MCの観測bar returnや252終値highとは窓・端点が異なり、同じ価格の重複した支持票にしない。新peer比較は含まない。未評価の`pre_anchor60`は初版へ追加していない。

## Investment Judgmentへの接続

```bash
uv run baibai-engine technical-analysis show --ticker 7203 --asof 2026-10-16
```

観測日cutoffは守るが、後日受領や訂正前vintageまで復元するintraday PITではない。新しいInvestment Judgmentは固定generationからtechnical_analysisのモデル結果をSelectionへ複写し、domain-inputs.yamlのsecuritiesへ投影する。blockのない旧snapshotへ後付け再計算しない。

MA・BB・RSI・騰落率は同じ価格を加工した情報で、独立票ではない。BB下側・低RSIを買い、上側・高RSIを売りと自動解釈せず、価値差と価格局面を[銘柄評価](stock-assessment.md#analysis-domain-integration)で接続する。
