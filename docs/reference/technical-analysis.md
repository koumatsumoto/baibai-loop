# テクニカル分析

保存価格から位置・勢い・変動幅を読む独立L2領域。`machine_analysis/technical_analysis/`が計算・query・CLIを所有し、[共有価格basis](../../engine/src/baibai_engine/market/price_basis.py)を使う。企業価値・候補順位・合成score・自動売買を作らず、新しいsourceやstoreを持たない。

## 入力と解釈

raw closeをas-of以下の価格調整factorで揃えたprice-only系列で、現金配当を含まない。close欠損日のactionも調整から落とさず、終値は補間・前日値補完しない。bar数は観測数であり連続営業日数ではない。計算可能は鮮度・流動性・取引可能性の保証ではなく、不足指標だけを未評価にする。

SMA20/50/200は現在価格との位置・並び・乖離を読む。期間は簡潔な慣例で、日本株での最適性を実証した値ではない。傾きや新規crossを推測しない。BB20の帯は価格分布の記述で、正規性、95%包含、反転確率は仮定しない。

Wilder RSI14は保存済み全履歴のseedと再帰に依存する。履歴の開始・本数・seed情報を確認し、直近14差分の単純平均と混ぜない。完全flatの50は計算上のconventionで、独立した中立判断ではない。履歴延長やprovider訂正で計算値は変わり得る。

12–1か月momentumは共通営業日のP(t−21)/P(t−252)−1で、直近月を除く価格変化。共通calendarの必要範囲に穴があれば欠測とし、個社の欠測端点を近隣価格へずらさない。SMA・RSI等の個社観測bar窓とは別の時間軸である。

## Investment Judgmentへの接続

```bash
uv run baibai-engine technical-analysis show --ticker 7203 --asof 2026-10-16
```

観測日cutoffは守るが、後日受領や訂正前vintageまで復元するintraday PITではない。新しいInvestment Judgmentは固定generationからtechnical_analysisのモデル結果をSelectionへ複写し、domain-inputs.yamlのsecuritiesへ投影する。blockのない旧snapshotへ後付け再計算しない。

MA・BB・RSI・騰落率は同じ価格を加工した情報で、独立票ではない。BB下側・低RSIを買い、上側・高RSIを売りと自動解釈せず、価値差と価格局面を[銘柄評価](stock-assessment.md#analysis-domain-integration)で接続する。
