---
title: "buyback trailing carry と取得枠の point-in-time 診断"
summary: "3m/6m診断では取得枠とcompositionが正方向だが、1yは両群比較不能、3y/5y production evidence不成立のためE[r]は変更しない。"
doc_type: report
status: active
date: 2026-08-09
---

# buyback trailing carry と取得枠の point-in-time 診断

価値tier: T1 — 終了済み取得枠の trailing 株数減少が E[r] を押し上げる偏りと、新規枠の過小評価を同じ長期座標で識別し、候補順位の精度を上げる。

## 結論

**判定は `non_adoption`。production の E[r]、ranking、gate は変更しない。**

Form 220 の取得枠状態を calibration panel へ point-in-time 結合すると、3m の positive−non-positive 年率中央値差は trailing share change +18.29pt、authorization pace +16.59pt、composition +16.59ptだった。6m はそれぞれ +13.17pt / +19.24pt / +19.63ptで、短期 regression alert は悪化していない。

一方、1yの共通 resolved row 1,368件は authorization / composition の positive 群が0で比較不能、3y / 5yの共通 rowと完全な point-in-time cohortも0である。短期の良い結果を長期証拠へ格上げしない。shortlist では取得期間、残枠、取得目的、消却を一次開示で確認し、終了済み carry を「これから受け取る還元」とする narrative を剥がす現行手順を維持する。

## 1. 実行契約

実行コマンド:

```bash
.venv/bin/python -m tools.experiments.measure_buyback_authorization \
  --out .cache/buyback-authorization-calibration.yaml
```

- calibration panel: 2020-01-31〜2026-06-30、78 cohort、流動性通過110,341 row
- Form 220 / 230 document list: 2025-08-01〜2026-08-07、6,687 row
- parsed report: 2025-08-08〜2026-08-07、6,067 row
- realized basis: `price_return_only`
- group minimum: positive / non-positive 各10 row / cohort
- point-in-time gate: `filed_on <= asof` かつ `report_month_end <= asof`
- no-filing gate: finalな日次EDINET全件一覧、metadata件数、永続行数、`source_coverage`が一致してas-ofから365日連続

報告月末だけを条件にすると、翌月提出の内容を提出前as-ofへ混入する。readerは提出日も条件にし、訂正報告で上書きされた原本を過去へ復元できない場合は、訂正後の値を過去へ流用せず行を欠損にする。

## 2. 同一 cohort・同一 buyback component の比較

3 variant は、いずれかの signal が欠損する row を全 variant から除き、同じ resolved rowだけで比較した。

| variant | 3m common row / cohort | 3m delta / positive cohort | 6m common row / cohort | 6m delta / positive cohort |
| --- | ---: | ---: | ---: | ---: |
| trailing share change | 14,078 / 10 | +18.29pt / 10 | 9,604 / 7 | +13.17pt / 7 |
| authorization pace | 14,078 / 9 | +16.59pt / 8 | 9,604 / 6 | +19.24pt / 6 |
| composition | 14,078 / 9 | +16.59pt / 9 | 9,604 / 6 | +19.63pt / 6 |

signal は次のように固定した。

- trailing: `clip(-net_share_change_yoy, -5%, +5%)`
- authorization: active 枠の `clip(4 × trailing_3m_acquired_ratio, 0, 5%)`。ended / no filing は0、状態・ペース欠損はnull
- composition: active は trailing と authorization の大きい方、ended / no filing は0、状態欠損はnull

composition は trailing と新規枠を加算しない。同じ取得が時間差で両方へ現れるためである。残枠比率は取得余地であって年率carryではないので、ペース欠損の代用品にしない。

## 3. authorization status 別 coverage と realized return

3m の全流動性母集団で、status別の realized年率中央値と同cohort母集団に対するexcessを出した。

| status | eligible / resolved | cohort | realized annualized | excess |
| --- | ---: | ---: | ---: | ---: |
| active | 2,082 / 1,779 | 9 | +20.66% | +9.73pt |
| ended | 2,725 / 1,898 | 9 | +11.23% | +5.56pt |
| filing state unresolved | 1,280 / 1,001 | 9 | +12.16% | +1.12pt |
| no filing | 13,314 / 11,389 | 10 | +8.20% | −2.60pt |
| unknown | 90,940 / 90,596 | 66 | +5.47% | 0.00pt |

ended 群も短期では母集団を上回った。したがって「枠終了だから株数減少signalを無価値とする」とは言えない。終了済みcarryをshortlist narrativeでforward cash returnから外すことと、production式からsignalを削ることは別判断である。

Form 220 / 230 の positive filing は2025-08-01開始だが、全様式を含むfinalなEDINET日次一覧は2024-07-31から連続している。したがって2025-07以降のcohortでは、Form 220が無い銘柄を`no_filing`として識別できる。特定様式のsentinel提出だけではuniverse全体のcoverageとせず、途中の欠落・partial・一覧件数不一致・未確定日は`unknown`へ倒す。

## 4. 消却・再放出目的

Form 220の「処理状況」は、カテゴリ見出しの存在ではなく、報告月に正の株数を伴う実行行だけを分類する。

| group | 3m eligible / resolved | realized return |
| --- | ---: | ---: |
| employee compensation / ESOP | 0 / 0 | — |
| cancellation | 0 / 0 | — |
| other re-release | 0 / 0 | — |

calibration as-ofとローカルsource ZIPが重なる行では、完全観測0、部分観測1、ZIP未観測5,700、報告自体なし104,640だった。したがって3群の0件は「該当なし」ではなく**目的未観測**であり、比較結果を主張しない。また処理状況は実行済みの消却・再放出で、取締役会が将来の取得目的として明示した内容ではない。shortlistでは引き続き会社の一次開示を確認する。

## 5. horizon authority と採否

| horizon | authority | common resolved | complete point-in-time cohort | 判定 |
| --- | --- | ---: | ---: | --- |
| 3m | regression alert | 14,078 | 0 | 短期の退行なし。採用根拠にしない |
| 6m | regression alert | 9,604 | 0 | 短期の退行なし。採用根拠にしない |
| 1y | leading evidence | 1,368 | 0 | authorization / compositionのpositive群なし |
| 3y | production decision evidence | 0 | 0 | block |
| 5y | production decision evidence | 0 | 0 | block |

production adoption の blocker は `no_complete_3y_authorization_cohort` と `no_complete_5y_authorization_cohort`。完全cohortはresolved survivorだけでなく対象identity全件、比較両群、point-in-time sourceを要求する。このartifactは必要条件の成立しか判定せず、将来それらが揃っても自動採用しない。現行method identityと本較正契約の事前登録・design/confirm・coverage gateで別途判断する。

## 6. この結果が意味しないこと

- 3m / 6m は月次forward窓が重なり、年率換算は短期変動を拡大する。cohort数を独立標本数やtrack recordと読まない。
- `price_return_only` は配当とbuyback cash flowを直接観測しない。ここで測るのはsignal群の価格return差である。
- active が ended より短期で上回ったことを、authorizationの因果効果とは呼ばない。valuation、sector、momentum等の交絡を除いていない。
- Form 220の処理状況は取得目的そのものではない。employee compensation / ESOP向け再放出と消却は一次IRで確定する。
- production式を変えない判定は、終了済みcarryをshortlistでforward還元として説明してよいという意味ではない。

Related: #867, #866
