# screening/mechanical.md

Baibai-Loop の **狭義のスクリーニング**（機械的ふるい）の仕様。4 成分アーキテクチャの (b) `records/03-candidates/` の出力を決める signal lane ベース rule。

## 1. 位置付け

- 4 成分アーキテクチャの **(b) records/03-candidates/** の中核
- universe（[`universe-rules.md`](./universe-rules.md)）× valuation / cash / CF / sales 指標（[`valuation-metrics.md`](./valuation-metrics.md)）を入力
- **通過銘柄 list を事実として出力**（解釈は入れない）
- research 選定の input となる

## 2. 入力

### 2.1 Universe

- 時価総額 100 億円以上
- 20 営業日平均売買代金 1 億円以上
- 上場 182 日以上
- 特別注意 / 整理銘柄 / 取引停止 / 上場廃止警告を除外
- 詳細: [`universe-rules.md`](./universe-rules.md)

### 2.2 指標

- PER（forward 優先、会社予想ベース、未公表時は trailing のみ）
- PBR
- EV/EBITDA（EDINET 前処理済み metrics がある場合のみ）
- P/S
- PCFR
- OCF yield（CFO TTM / market cap）
- cash-to-market-cap / price-to-equity
- 業種中央値（東証 33 業種、n<10 は市場全体 fallback）
- 過去 3 年自己レンジ（上場 3 年未満は上場来）
- 詳細: [`valuation-metrics.md`](./valuation-metrics.md)

## 3. Signal lane（OR 条件、最低 1 つ満たす）

以下の signal lane のうち、**最低 1 つ** を満たす銘柄を通過とする。閾値は `records/_config/screening-rules.yaml` を正本とする。

### 3.1 `valuation-reversion`

伝統的な valuation mean-reversion。旧来の 3 条件を 1 つの lane に束ね、`reasons[]` で内訳を残す。

- 業種中央値比 + 過去自己レンジ下位
- 過去 60 営業日の急落 + valuation 下方乖離
- セクターローテーションによる短期売り

EDINET が無い場合、EV/EBITDA は `unavailable` として判定対象から外す。PER / PBR / P/S など利用可能な指標で degrade して評価する。

### 3.2 `cash-rich-asset-discount`

CashEq / market cap と price-to-equity を使い、厳密 net cash ではないが、cash-rich / asset discount 候補を拾う。J-Quants 財務サマリーのみで完結させ、有利子負債は research で一次確認する。

### 3.3 `cashflow-yield-discount`

期間正規化した CFO TTM から OCF yield を算出し、営業 CF がプラスで、CF 悪化が大きくない銘柄を拾う。TTM が作れない銘柄はこの lane から除外する。

### 3.4 `sales-discount-growth`

P/S が業種中央値比で安く、売上成長が残る銘柄を拾う。営業赤字銘柄は CFO プラスまたは営業赤字縮小が確認できる場合に限り許容する。

### 3.5 OR 条件の意味

- **最低 1 つ満たせば通過**
- 複数 signal が重なる銘柄は research 優先度を上げる
- candidates YAML の `signals[]` に lane 名、playbook、hit reasons、判定に使った metrics を記録する

## 4. 出力

### 4.1 Path

```
records/03-candidates/YYYY/MM/YYYY-MM-DD.yaml
```

1 実行 = 1 ファイル（週次運用）

### 4.2 YAML

詳細は [`../components/candidates.md`](../components/candidates.md) 節 4 を参照。核心:

```yaml
candidates:
  - ticker: "7203"
    name: "..."
    per_forward: 8.2
    pbr: 0.72
    p_s: 0.6
    pcfr: 5.1
    metrics:
      ocf_yield: 0.13
      cash_to_market_cap: 0.42
    signals:
      - name: cashflow-yield-discount
        playbook: cashflow-yield-discount
        reasons: [ocf_yield_discount]
```

## 5. 実行頻度

- **週次 1 回**（初期値、retro で調整）
- 実行曜日: 毎週月曜朝 or 金曜夕方（運用で決める）

## 6. 実装方針

CLI で自動化されており、実装の正本は [`automation.md`](./automation.md) を参照。本ドキュメントは mechanical ルールの意味論に絞る。

```bash
python -m baibai_loop.screening.cli run --asof YYYY-MM-DD
```

## 7. Retro での調整

月次 retro で以下を評価:

- **signal lane 別 hit 数**: 多すぎる / 少なすぎる場合は `screening-rules.yaml` の閾値調整候補
- **採用率**: 通過銘柄のうち research で採用された割合
- **skipped trade log**: 見送り銘柄の事後パフォーマンス
- **lane 別の成功 / 失敗分類**: どの割安タイプが機能したか

閾値変更は playbook 改訂議論に含める。

## 8. 事実と分析の分離

- mechanical は **事実層**。閾値適用・signal hit は機械的
- 「なぜ割安か」の仮説・「採用すべきか」の判断は research 側
- candidates ファイル本文には補足情報を事実として記録し、解釈を入れない

## 9. 参考

- [`principles.md`](./principles.md): スクリーニング原則
- [`universe-rules.md`](./universe-rules.md): universe 境界条件
- [`valuation-metrics.md`](./valuation-metrics.md): 指標算出仕様
- [`macro-gate-procedure.md`](./macro-gate-procedure.md): research 側の Macro gate
- [`../components/candidates.md`](../components/candidates.md): candidates 運用仕様
- [`../components/research.md`](../components/research.md): research 選定プロセス
