# screening/mechanical-v1.md

Baibai-Loop の **狭義のスクリーニング**（機械的ふるい）の仕様。4 成分アーキテクチャの (b) `screened/` の出力を決める閾値ベース rule。

## 1. 位置付け

- 4 成分アーキテクチャの **(b) screened/** の中核
- universe（[`universe-rules.md`](./universe-rules.md)）× valuation 指標（[`valuation-metrics.md`](./valuation-metrics.md)）を入力
- **通過銘柄 list を事実として出力**（解釈は入れない）
- research 選定の input となる

## 2. 入力

### 2.1 Universe

- 時価総額 300 億円以上
- 20 営業日平均売買代金 2 億円以上
- 上場 6 か月以上
- 特別注意 / 整理銘柄除外
- 詳細: [`universe-rules.md`](./universe-rules.md)

### 2.2 Valuation 指標

- PER（forward 優先、会社予想ベース、未公表時は trailing のみ）
- PBR
- EV/EBITDA
- P/S
- PCFR
- 業種中央値（東証 33 業種、n<10 は市場全体 fallback）
- 過去 3 年自己レンジ（上場 3 年未満は上場来）
- 詳細: [`valuation-metrics.md`](./valuation-metrics.md)

## 3. 閾値条件（OR 条件、最低 1 つ満たす）

以下 3 種の閾値条件のうち、**最低 1 つ** を満たす銘柄を通過とする（OR 条件）。

### 3.1 条件 A: 業種中央値比 + 過去自己レンジ下位

- PER / PBR / EV-EBITDA のいずれかが **業種中央値比 -20% 以上の水準**
- かつ、**同指標が過去 3 年自己レンジの下位 20%** に入っている
- 両方を同時に満たすことが必要（業種対比と自己対比の二重確認）
- 注: **EV/EBITDA は issue #15 の historical 近似バグの修正までは A/B 判定から一時除外**している。実装上は PER / PBR のみで評価する。

### 3.2 条件 B: 過去 60 営業日の急落 + valuation 下方乖離

- 株価が過去 60 営業日で **-15% 以上** 下落
- かつ、PER / PBR / EV-EBITDA のいずれかが **1σ 以上下方に振れている**（過去 3 年平均 + 標準偏差ベース）
- 業績トレンドに明確な悪化がない（EPS / ROE / 売上の前年比が大きく崩れていない）
- 注: EV/EBITDA は 3.1 と同様に issue #15 の解決までは対象外（PER / PBR のみ）。

### 3.3 条件 C: セクターローテーションによる短期売り

- 業種 relative strength が直近 4 週で **下位 20%** に入っている
- かつ、個別銘柄が業種平均を下回って売られている（業種下落幅を超える下落）
- かつ、業績トレンドに明確な悪化がない

### 3.4 OR 条件の意味

- **最低 1 つ満たせば通過**（複数満たす銘柄は confidence が高い）
- 通過した銘柄の front matter `threshold_hit` に「どの条件を満たしたか」を記録
- research 側で primary metric と合わせて採用判定の input にする

## 4. 出力

### 4.1 Path

```
screened/YYYY/MM/YYYY-MM-DD.md
```

1 実行 = 1 ファイル（週次運用）

### 4.2 Front matter

詳細は [`../components/screened.md`](../components/screened.md) 節 4 を参照。核心:

```yaml
tickers:
  - ticker: "7203"
    name: "..."
    per_forward: 8.2 | null
    per_trailing: 9.5
    pbr: 0.72
    ev_ebitda: 4.8
    p_s: 0.6
    pcfr: 5.1
    sector_33: "輸送用機器"
    threshold_hit:
      - sector_median_under_20pct_and_self_range_bottom_20pct  # 条件 A
      - price_down_60d_and_valuation_sigma_down                # 条件 B
      - sector_rotation_short_sell                             # 条件 C
```

## 5. 実行頻度

- **週次 1 回**（初期値、retro で調整）
- 実行曜日: 毎週月曜朝 or 金曜夕方（運用で決める）

## 6. 実装方針

v1 は CLI で自動化しており、正本の実装仕様は [`automation-v1.md`](./automation-v1.md) を参照。

- 実行形式: `python -m baibai_loop.screening.cli run --asof YYYY-MM-DD`
- 本ドキュメントは mechanical ルール（閾値条件・rule engine）の意味論に絞り、実行方式・実装構成の詳細は automation-v1.md を正本とする
- retro で以下が安定したら閾値・データソースを調整する:
  - 閾値の妥当性（false positive/negative 評価）
  - データソースの安定性（J-Quants / EDINET 取得失敗の頻度）
  - 業種分類粒度（33 業種で十分か）

## 7. Retro での調整

月次 retro で以下を評価:

- **閾値通過銘柄数**: 多すぎる（選定が困難）/ 少なすぎる（候補不足）場合は閾値調整候補
- **採用率**: 通過銘柄のうち research で採用された割合。低すぎる場合は閾値が甘い
- **skipped trade log**: 見送り銘柄の事後パフォーマンス。「割安判定したが採用見送り → 上昇」の偽陰性率

閾値変更は playbook v2 改訂議論に含める（v1 運用中は据え置き、#7 から継承）。

## 8. 事実と分析の分離

- mechanical-v1 は **事実層**。閾値適用・threshold_hit は機械的
- 「なぜ割安か」の仮説・「採用すべきか」の判断は research 側
- screened ファイル本文には補足情報（実行時の市場環境メモ、除外した特殊ケース等）を事実として記録。解釈を入れない

## 9. 参考

- [`principles.md`](./principles.md): スクリーニング原則
- [`universe-rules.md`](./universe-rules.md): universe 境界条件
- [`valuation-metrics.md`](./valuation-metrics.md): 指標算出仕様
- [`macro-gate-procedure.md`](./macro-gate-procedure.md): research 側の Macro gate
- [`../components/screened.md`](../components/screened.md): screened 運用仕様
- [`../components/research.md`](../components/research.md): research 選定プロセス
