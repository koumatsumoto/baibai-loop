---
name: market-context-assessment
description: 人間の明示開始から市場入力を固定し、重要な環境変化と含意を独立検証後に発行する。
---

# 市場環境判断（Market Assessment）

明示依頼で固定時点の水準・変化・反証/不確実性を個社の耐性・仮定・価格へつなぐ。L2の[Reading](../../../docs/reference/model-observations.md#reading)とは別で、日次更新/staleで起動せず第二正本にしない。章数/確率格子/scenario件数を課さず不足や小変化も扱う。

独自risk点・指数連動要求率・自動配分・一律業種除外/売却は[比較の結果](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-20-macro-risk-strategy/report.md)未採用であり、集約指数一般の無効や現在の市場状態の認定ではない。

## 入力・作成・発行

[applicationの作業DBと反映手順](../../../jobs/OPERATIONS.md#application-db-を反映する)を使う。確認と発行は同じ作業DBを指定し、L2の指標公開ではL3の判断を反映しない。新しい変化の主張に出典を挙げるなら、欠測・取得失敗だけでは裏付けにならない。確認できなかったことは不確実性として残す。

1. 対象日as_ofと入力確定時刻known_atを固定し、保存Reading・市場/業種/個社状態と一次資料を読む。[補助観測](../../../jobs/OPERATIONS.md#判断直前の多市場補助観測)も時点/用途を守り、同じsource-handoffをreviewerへ渡す。
2. 個社への含意を記し、別sessionのreviewerがsource・因果・反証を確認する。数値/単位/日付/限定を保ち、新根拠/判断は再Review。最大3巡で未解決なら発行せず人間へ上げる。
3. Review PASSまたは人間のdraft承認後、known_atを保ち、初回発行直前の実時刻を本文のpublished_atに記録する。check→head確認→CAS発行。checkは本文の構造確認だけで、headや独立検証の代わりではない。

```bash
uv run baibai-engine investment-judgment market-context --db <APP_DB> publish <DRAFT> --check
uv run baibai-engine investment-judgment market-context --db <APP_DB> head
uv run baibai-engine investment-judgment market-context --db <APP_DB> publish <DRAFT> --expected-head <CONTEXT_ID>
```

expected-head省略はheadがnullの場合だけ。再送は同ID/内容/published_atを保つ。結果不明はIDで読戻し、CAS競合でheadだけ差替えない。発行後は本文を読戻して反映する。

```bash
uv run baibai-engine investment-judgment market-context --db <APP_DB> show --context-id <CONTEXT_ID>
uv run baibai-engine investment-judgment market-context --db <APP_DB> latest --asof <YYYY-MM-DD> --known-at <AWARE_DATETIME>
```

## 選択と履歴

新しい保存形式では、対象日がas_of以前で、実際の発行受付が照会のknown_atまでに済んだ一件を選ぶ。本文のpublished_atを前に書いても、発行受付を早めた扱いにはならない。旧本文は申告された時刻で照会・選定に現れることがあるが、当時すでに利用できたことを実受領で証明するものではない。週末の受付を対象日末で切らず、提供元の公表時刻を取得時刻に読み替えない。

個社判断へ渡す保存判断の情報締切は、その発行時刻や今回Selectionの締切とは別に読む。未収録の旧本文では不明のままとし、発行までの新情報を確認済みへ補完しない。旧Selectionの再開は元の固定投影を保持する。

exact ID/historyは保存本文・入力・引用・supersedesを検証し、新writer/cutoff/projectionで書換えない。旧抽出を新判断の必須構造にせず、旧runner入力がなければ本文の範囲だけを示す。latestは選択一件の不整合を旧値で埋めず、全L3履歴検査を前提にしない。
