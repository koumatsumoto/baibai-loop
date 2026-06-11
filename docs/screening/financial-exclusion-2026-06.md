---
title: "valuation-reversion 金融業除外の計測 (2026-06)"
summary: "valuation-reversion lane への金融 4 業種 excluded_sectors 追加を、candidates 再生成 + replay / lane cohorts / 本番 select 比較で検証した記録。"
doc_type: reference
status: active
last_reviewed: 2026-06-12
related_docs:
  - "./lane-cohorts-2026-05.md"
  - "./tuning-2026-06.md"
  - "./mechanical.md"
---

# valuation-reversion 金融業除外の計測 (2026-06)

`#230` の運用試験。valuation-reversion lane に金融 4 業種(銀行・証券・保険・その他金融)の `excluded_sectors` を追加した config(`records/_config/screening-rules/2026-06-12T000000+0900.yaml`)を、旧 config(2026-06-10 版)と同一コード・同一データで比較した。変更はメカニズム根拠(金融業の PER / PBR は規制資本・金利環境の構造要因を含み、事業会社の mean-reversion 前提で機械判定できない。他 5 lane は同根拠で除外済み)に基づき、閾値の grid search は行っていない。

## 方法

- 旧 / 新 config で 2026-05-08 / 05-15 / 05-29 / 06-08 の candidates を再生成(`.cache/replay/candidates-base` / `candidates-finx`、EDINET / JPX snapshot は SQLite 既存分、`--allow-stale-jpx`)
- `screening-replay`(regime lens on、balanced、top5、holdout = 06-08)と `lane-cohorts`(1w / 4w)を両 root で実行。eval cap `2026-06-11`
- 本番相当の `select`(macro context 適用)を 2026-06-08 candidates の旧 / 新で比較

## 結果

### Screen 出力(事実層)

- 除外された銘柄は週あたり 5〜8(全 candidates の 0.4%)。全て valuation-reversion **単独** hit の金融銘柄(8303 / 7388 / 8584 / 8585 / 462A / 7196 / 8518 / 8783 / 7347)

### Replay(macro-agnostic、推奨 queue)

- **4 週すべてで推奨 queue が完全一致**(劣化なし)。金融銘柄は macro-agnostic replay では queue 上位に届いていなかったため、replay 上の改善も劣化も発生しない

### Lane cohorts(valuation-reversion cohort、mean relative)

| 週 | horizon | 旧 | 新 | Δ |
| --- | --- | ---: | ---: | ---: |
| 2026-05-08 | 1w | +0.73 | +0.86 | +0.13 |
| 2026-05-08 | 4w | -8.29 | -8.01 | +0.28 |
| 2026-05-15 | 1w | -2.79 | -2.75 | +0.04 |
| 2026-05-29 | 1w | -1.70 | -1.71 | -0.00 |

方向は改善側だが、cohort 規模(299〜604)に対し除外は 5〜8 銘柄のため効果は小さい。

### 本番 select(macro context 適用、2026-06-08)

実質的な効果はここに出る。macro tailwind 整合が sort key の先頭成分のため、金融銘柄が rank 1-2 を占有していた:

- 旧: **8303 ＳＢＩ新生銀行(銀行業) / 7388 ＦＰパートナー(保険業)** が rank 1-2。#228 の人間レビューで「研究上の留保が大きい(bars 取引ギャップ、PER 181 倍)」と判定され、research コストを消費した
- 新: rank 1-2 が **2220 亀田製菓 / 4569 杏林製薬**(事業会社、long_hold high)に置き換わり、rank 3-5(6104 / 4061 / 6039)は不変

## 判定

採用条件(tuning-2026-06 と同じ)に対して: (a) lane cohort は小幅改善、本番 select は研究 queue の品質改善 (b) replay 4 週で非劣化 (c) メカニズム説明可能(他 lane と同一根拠)。**採用**。

## 限界

- 2026-05〜06 の 4 週・小サンプル。金融セクターが主導する相場(銀行株ラリー等)では、この lane から金融を拾えないことが機会損失になる可能性がある。その場合は research が macro context の sector 優先度から手動で拾う(screen は他 lane 同様、機械判定が成立する母集団に限定する方針)
- 06-08 週の 1w forward は eval cap 未到達で未解決。forward 蓄積後に lane cohort を再確認する(#157 の再計測と同時期)

## 運用メモ(再現時の注意)

- 過去 asof の `run` は、より新しい asof を先に bootstrap すると earnings calendar の exact coverage(coverage_start <= asof)を満たせなくなる。earnings calendar は live-only endpoint のため、この場合は設計済みの `--allow-stale-jpx` fallback(horizon coverage)を使う
- 再現コマンド:

```bash
SCREENING_RULES_PATH=records/_config/screening-rules/2026-06-10T000000+0900.yaml \
  uv run baibai-loop-screening run --asof <W> --allow-stale-jpx \
  --output-path .cache/replay/candidates-base/<Y>/<M>/<W>.yaml --force
uv run baibai-loop-screening run --asof <W> --allow-stale-jpx \
  --output-path .cache/replay/candidates-finx/<Y>/<M>/<W>.yaml --force
uv run baibai-loop-ledger screening-replay --candidates-root .cache/replay/candidates-finx \
  --holdout-weeks 1 --regime-lens on --out .cache/replay/replay-finx-202606.yaml
uv run baibai-loop-ledger lane-cohorts --candidates-root .cache/replay/candidates-finx \
  --horizons 1,4 --out .cache/replay/lane-cohorts-finx-202606.yaml
```
