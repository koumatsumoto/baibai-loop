---
title: "Market regime lens replay 検証 (2026-05)"
summary: "market regime lens の on/off を 2026-05 の 4 週に replay し、fast_dislocation boost 中立化の forward return 影響を比較する。"
doc_type: reference
status: active
last_reviewed: 2026-06-10
related_docs:
  - "./replay-2026-05.md"
  - "./mechanical.md"
---

# Market regime lens replay 検証 (2026-05)

`#204` / `#211` の market regime lens の運用試験。regime lens の on / off で同一の週次 candidates を replay し、recommended queue（各 profile 5 銘柄）の forward return を日経225 ETF proxy（`1321`）比で比較した。算出方法・価格基準・サンプル制約は [`replay-2026-05.md`](./replay-2026-05.md) と同一（macro-agnostic、eval cap `2026-06-08`）。

## Lens の仕様（検証対象）

- regime snapshot: benchmark `1321` の 20 営業日リターン（adjusted close）で分類。`risk_on_rally` = +3% 以上 / `risk_off_selloff` = -3% 以下 / その他 `neutral_range`。breadth（20 本 MA 上抜け銘柄比率）は事実として記録のみ
- 効果: `risk_on_rally` の週だけ selection sort key の fast_dislocation boost を中立化。候補除外はしない

## 分類定義の改訂経緯（透明性のための記録）

初期案は「trend +3% **かつ** breadth 55% 以上」を rally 条件としていたが、2026-05 の 4 週は全週 breadth 29.6〜44.7% の**狭いラリー**（benchmark 20bd +8.7〜+17.4%）であり、初期案では全週 `neutral_range` となって観測済みの failure regime（oversold tilt の rally 劣後）を一切拾えなかった。

fast_dislocation は anti-momentum 銘柄を選ぶため、劣後メカニズムは指数モメンタムの強さに依存し、ラリーの広狭には依存しない。この仮説に基づき rally 条件を trend 単独（+3%）に改訂した。これは閾値の grid search ではなく分類軸の修正だが、**検証データを見た後の 1 回の改訂である**ことをここに明記する。最終判断は今後の forward 運用で検証する。

## 週別 regime 判定

| 週 | regime | benchmark ret 20bd | breadth |
| --- | --- | ---: | ---: |
| 2026-05-01 | risk_on_rally | +13.27% | 0.296 |
| 2026-05-08 | risk_on_rally | +17.35% | 0.385 |
| 2026-05-15 | risk_on_rally | +8.65% | 0.389 |
| 2026-05-29 (hold-out) | risk_on_rally | +9.32% | 0.447 |

## 結果（mean relative、lens ON vs OFF）

| 週 | profile | 1w ON | 1w OFF | 4w ON | 4w OFF |
| --- | --- | ---: | ---: | ---: | ---: |
| 05-01 | strict | -5.98 | -6.91 | -10.94 | -12.26 |
| 05-01 | balanced | -5.91 | -5.84 | -10.39 | -10.42 |
| 05-01 | loose | -6.58 | -6.03 | -9.64 | -9.90 |
| 05-08 | strict | **+5.66** | -1.98 | **+3.95** | -6.29 |
| 05-08 | balanced | **+6.94** | +2.60 | **+4.02** | -11.12 |
| 05-08 | loose | +1.18 | +1.13 | -1.17 | -5.58 |
| 05-15 | strict | +2.90 | -1.34 | n/a | n/a |
| 05-15 | balanced | -1.23 | +3.07 | n/a | n/a |
| 05-15 | loose | -1.89 | -4.12 | n/a | n/a |
| 05-29 (hold-out) | strict | -0.89 | -1.09 | n/a | n/a |
| 05-29 (hold-out) | balanced | -1.71 | -2.05 | n/a | n/a |
| 05-29 (hold-out) | loose | -1.58 | -3.16 | n/a | n/a |

集計（profile-week 平均）:

- 1w mean relative: **ON -0.76pt vs OFF -2.14pt**（+1.38pt 改善、n=12）
- 4w mean relative: **ON -4.03pt vs OFF -9.26pt**（+5.23pt 改善、n=6）
- hold-out 週（05-29）は 3 profile すべてで ON が改善
- 悪化セル: 05-01 balanced 1w（-0.07pt）、05-01 loose 1w（-0.55pt）、05-15 balanced 1w（-4.30pt）の 3 / 18

## 解釈と限界

1. **方向整合は確認できた**。lens ON は 18 セル中 15 で改善し、特に 4w の改善幅が大きい。boost 中立化により lane rank / long_hold が優先され、急落直後の銘柄が推奨 queue の先頭から外れた効果と整合する
2. **この検証は in-sample 性を持つ**。「oversold tilt が rally で劣後する」という仮説自体が 2026-05 の観測から生まれており、同じ期間での確認は仮説生成データの再利用である。さらに分類定義の改訂（trend 単独化）も同期間の観測を踏まえている。**「2026-05 に有効だった」以上の結論を出さない**
3. **selloff / neutral 局面の挙動は未検証**（2026-05 に該当週がない）。設計上は従来挙動と完全一致するため、リスクは追加していない
4. 判定が必要なのは forward 検証: 今後の週次 screening 運用で regime 判定と推奨 queue の forward return を蓄積し、2026-07 retro 以降で lens の採否を再評価する

## 再現手順

```bash
uv run baibai-loop-ledger screening-replay \
  --candidates-root .cache/replay/candidates --holdout-weeks 1 \
  --regime-lens on --out .cache/replay/replay-regime-on.yaml
uv run baibai-loop-ledger screening-replay \
  --candidates-root .cache/replay/candidates --holdout-weeks 1 \
  --regime-lens off --out .cache/replay/replay-regime-off.yaml
```
