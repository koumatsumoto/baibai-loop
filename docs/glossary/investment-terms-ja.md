---
title: "投資判断用語集"
summary: "Baibai-Loop canonical investment vocabulary in Japanese."
doc_type: glossary
status: active
last_reviewed: 2026-05-06
related_docs:
  - "../concepts.md"
---

# 投資判断用語集

## Portfolio Policy / IPS / Mandate

`portfolio policy` は、自己運用における目的、制約、資本、許容リスク、time horizon、eligible universe、liquidity constraints、kill switch をまとめる統制文書です。CFA の IPS に近い考え方ですが、Baibai-Loop は投資助言サービスではなく、自己判断を後から検証するための audit discipline として使います。

`policy snapshot` は research / trade / ledger に記録された、その判断時点の policy assumptions です。あとで policy が変わっても、当時の判断条件を再現できるようにします。

`mandate` は委任運用の語感が強いため、Baibai-Loop の正準語にはしません。外部文献の説明では使えますが、repo 内の canonical label は `portfolio policy` です。

## Evidence Family

`evidence family` は Baibai-Loop 内部の evidence taxonomy です。Systematic risk factor や Fama-French factor とは別概念です。Investment memo で thesis を検証するための analytical lens / return driver に近い語です。

Artifact-level では次を扱います。

- `macroeconomic`
- `policy/geopolitical`
- `fundamental`
- `valuation`
- `market-derived`
- `positioning/liquidity`
- `catalyst`

Candidate-level evidence hit では、原則として `fundamental`, `valuation`, `market-derived`, `positioning/liquidity`, `catalyst` に限定します。`macroeconomic` と `policy/geopolitical` は macro regime 側で扱います。

## Technical / Market-Derived

`technical` は金融一般では technical analysis や technical signal を指します。ただし Baibai-Loop の正準 evidence family にはしません。

価格、相対強度、値動き、出来高など市場から観測される情報は `market-derived evidence` と呼びます。外部引用や glossary 上で `technical indicator` と書くことは許容しますが、schema-visible concept と canonical prose では `market-derived` を使います。

## Positioning / Liquidity

`positioning / liquidity evidence` は、short interest、信用残、turnover、ADV、特別注意銘柄、流動性制約などを扱います。

`crowding` と呼ばれがちな需給の混雑は、この下位概念として扱います。単独の正準カテゴリにはしません。

## Signal / Evidence Hit

`signal` は金融一般では technical signal や model output として広く使われます。Baibai-Loop の schema-visible concept としては正準語にしません。

Baibai-Loop では `evidence hit` を使います。`evidence hit` は candidate / investment memo に現れる、source と provenance を持つ証拠単位です。

`independent evidence count` は、position sizing や conviction の説明で使う相関調整後の証拠数です。単純な signal count ではありません。

## Playbook / Strategy / Underwriting

`playbook` は、再現可能な投資判断パターンです。Screening rule、investment memo の論点、review attribution をつなぐ repeatable thesis pattern を指します。

`strategy` は repo 全体の forward-only decision process に近い語であり、個別 playbook の別名としては使いません。

`underwriting` は証券引受・保険引受の語感が強いため、二次市場の上場株スクリーニングでは使いません。

## Macro Regime / Macro Regime Gate

`macro regime` は outlook から来る市場環境の読みです。

`macro regime gate` は、その regime の下で当該候補が採用可能かを判断する eligibility gate です。`policy weight` は説明補助であり、validator-visible な採用可否と sizing cap は macro regime gate が担います。

## Security Exposure

`security exposure` は、個別銘柄がどの macro / sector / demand / FX / rate / commodity / cycle exposure を持つかを表す security-level mapping です。東証33業種だけで macro sensitivity を表現できない場合に使います。

## Position Sizing Overlay / Risk Budget

`position sizing overlay` は、個別候補の position sizing / timing cap に portfolio policy と macro regime を重ねる概念です。業界用語としての portfolio-level risk budget overlay とは区別します。

`paper proxy capital` は判断の強弱を比較するための仮想資本です。Real capital や tactical real budget とは別であり、実注文額は execution scaling から導出します。

## Investment Memo / Thesis Payoff

`investment memo` は `research` の user-facing 概念ラベルです。Directory / schema-visible fields では `research` を維持できます。

`thesis payoff` は entry、target、stop、expected upside / downside、risk/reward、time horizon、invalidation conditions を構造化した payoff view です。

## Decision Register

`decision register` は判断イベントを append-only に記録する register です。Baibai-Loop では `records/_ledger/` が research decision と tracking event の正本です。Candidates の screen fact、trades の execution record、reviews の attribution record とは分けます。

## Missed Opportunity / Screening False Negative

`missed opportunity tracking` は、見送り、保留、採用したが発注しなかった候補が後から良い relative return を出したかを追跡する概念です。

`screening false negative tracking` は、screening に入ったが hit しなかった、rank 外だった、または gate で落ちた universe member が後から相対的に良い成績を出したかを追跡する概念です。見送った候補だけでなく、screening rule 自体の取りこぼしを改善するために使います。

## Relative Return Attribution / Benchmark

`relative return attribution` は、review outcome を absolute return だけでなく market / sector baseline に対する relative return として評価することです。Bull / bear regime の beta を playbook alpha と誤認しないために使います。

`investment benchmark` は TOPIX や業種指数など投資成果の比較対象です。`regression benchmark` は schema / validator / migration の business invariants を検査する fixture 群です。両者は別概念です。
