---
name: market-context-assessment
description: 人間の明示開始から市場入力を固定し、重要な環境変化と含意を独立検証後に発行する。
---

# 市場環境判断（Market Assessment）

明示依頼で固定時点の水準・変化・反証/不確実性を個社の耐性・仮定・価格へつなぐ。L2の[Reading](../../../docs/reference/model-observations.md#reading)とは別で、日次更新/staleで起動せず第二正本にしない。章数/確率格子/scenario件数を課さず不足や小変化も扱う。

独自risk点・指数連動要求率・自動配分・一律業種除外/売却は[比較の結果](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-20-macro-risk-strategy/report.md)未採用であり、集約指数一般の無効や現在の市場状態の認定ではない。

## 入力・作成・発行

開始/再開・発行ID読戻し・クラウド反映は[application手順](../../../jobs/OPERATIONS.md#application-db-を反映する)へ。push-indicatorsではL3は反映されない。

1. 対象日as_ofと入力確定時刻known_atを固定し、保存Reading・市場/業種/個社状態と一次資料を読む。[補助観測](../../../jobs/OPERATIONS.md#判断直前の多市場補助観測)も時点/用途を守り、同じsource-handoffをreviewerへ渡す。
2. 個社への含意を記し、別sessionのreviewerがsource・因果・反証を確認する。数値/単位/日付/限定を保ち、新根拠/判断は再Review。最大3巡で未解決なら発行せず人間へ上げる。
3. Review PASSまたは人間のdraft承認後、known_atを保ち、初回発行直前の実時刻をpublished_atにしてcheck→head確認→CAS発行する。

```bash
uv run baibai-engine investment-judgment market-context publish <DRAFT> --check
uv run baibai-engine investment-judgment market-context head
uv run baibai-engine investment-judgment market-context publish <DRAFT> --expected-head <CONTEXT_ID>
```

expected-head省略はheadがnullの場合だけ。再送は同ID/内容/published_atを保つ。結果不明はIDで読戻し、CAS競合でheadだけ差替えない。発行後は本文を読戻して反映する。

```bash
uv run baibai-engine investment-judgment market-context show --context-id <CONTEXT_ID>
uv run baibai-engine investment-judgment market-context latest --asof <YYYY-MM-DD> --known-at <AWARE_DATETIME>
```

## 選択と履歴

as_of以下かつknown_atまでの実発行一件を選び、週末発行を対象日末cutoffで落とさない。provider公表時計をavailable_atへ捏造せず、新判断は実発行後のSelectionに固定する。

exact ID/historyは保存本文・入力・引用・supersedesを検証し、新writer/cutoff/projectionで書換えない。旧抽出を新判断の必須構造にせず、旧runner入力がなければ本文の範囲だけを示す。latestは選択一件の不整合を旧値で埋めず、全L3履歴検査を前提にしない。
