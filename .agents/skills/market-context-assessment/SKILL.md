---
name: market-context-assessment
description: 人間の明示開始から市場入力を固定し、重要な環境変化と含意を独立検証後に発行する。
---

# 市場環境判断（Market Assessment）

明示依頼で、固定入力/基準時点の重要な水準・変化・反証/不確実性を個社の耐性・仮定・支払価格へつなぐ。L2の[Reading](../../../docs/reference/model-observations.md#reading)とは別で、日次更新/staleだけでAI執筆を起動せず、L2値の第二正本を作らない。章数・確率格子・scenario件数を課さず、不足や小さな変化も扱う。

独自risk点・指数連動要求率・自動配分・一律業種除外/売却は増分価値と費用・誤除外の[方式比較](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-20-macro-risk-strategy/report.md)で未採用。集約指数一般の無効や現在の市場状態を認定するものではない。

## 入力・作成・発行

クラウド反映を伴う開始・再開は[applicationの作業手順](../../../jobs/OPERATIONS.md#application-db-を反映する)に従う。

1. 対象日as_ofと入力確定時刻known_atを固定し、保存Reading・市場/業種/個社状態と一次資料を読む。[多市場補助観測](../../../jobs/OPERATIONS.md#判断直前の多市場補助観測)も同手順の時点/用途を守り、同じsource-handoffをreviewerへ渡す。
2. 重要な変化と個社への含意を記し、別sessionのreviewerがsource・因果・反証を確認する。数値・単位・日付・限定を保ち、新根拠/判断を採ったら再Review。最大3巡で未解決なら発行せず人間へ上げる。
3. Review PASSまたは人間のdraft承認後、known_atを保ち、初回発行直前の実時刻をpublished_atとしてcheckし、headを読んでCAS発行する。

```bash
uv run baibai-engine investment-judgment market-context publish <DRAFT> --check
uv run baibai-engine investment-judgment market-context head
uv run baibai-engine investment-judgment market-context publish <DRAFT> --expected-head <CONTEXT_ID>
```

headがnullの場合だけexpected-headを省略する。同じIDは同内容の再試行に使い、published_atを作り直さない。結果不明ならIDで読戻し、CAS競合でheadだけを更新して再送しない。

```bash
uv run baibai-engine investment-judgment market-context show --context-id <CONTEXT_ID>
uv run baibai-engine investment-judgment market-context latest --asof <YYYY-MM-DD> --known-at <AWARE_DATETIME>
```

発行IDの本文を読み返して[application反映](../../../jobs/OPERATIONS.md#application-db-を反映する)へ進む。push-indicatorsだけではL3判断は反映されない。

## 選択と履歴

対象日以下かつknown_atまでに実発行された一件を選ぶ。週末発行を対象日末cutoffで落とさず、available_atに提供者の公表時刻を捏造しない。新判断は実発行後のSelectionへ固定する。

exact ID/historyは保存本文・入力・引用・supersedesを検証し、新writer/cutoff/projectionで旧構造を書き換えない。保存判断の抽出を新判断の必須構造にせず、旧runner入力がなければ本文から得た範囲だけを示す。latestは選んだ一件の不整合を旧正常値へfallbackせず、未選択のL3全履歴検査を先行条件にしない。
