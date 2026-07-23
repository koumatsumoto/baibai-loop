---
name: macro-analysis
description: 金利・為替・流動性・需要・資金調達・共通tail riskのmaterial changeが個別企業の5年評価を変え得るとき、macro contextを確認・更新するために使う。
---

# Macro Analysis

## 正本

最初に[`docs/workflow/macro.md`](../../../docs/workflow/macro.md)を読む。series、provider、8分析レンズ、source tier、record schemaをskillへ再転記しない。

## Trigger

- discount rate、需要、資金調達、common tailにmaterial changeがある。
- 主要event後、またはpublished contextのmonitoring conditionが発火した。
- 個別thesisのscenario/claimを変える外部経路を確認する。
- **opportunity cycleのshortlist作成前**: published contextがworkflowのDecision-grade深度契約を満たさない、またはas_of以降にmonitoring pointのdated eventを跨いだ場合はdecision-grade refreshを行う。

定期だからという理由だけでrecordを作らない。delta更新はmaterialでなければ根拠を短く返して終了する。decision-grade refreshは深度契約が基準であり、「変化が小さい」ことを浅い分析の理由にしない。

## 手順

1. `baibai-engine macro context head`で現行 context ID を、`context show --latest --asof <date>`で`as_of / valid_until / monitoring_points`を確認する。
2. 変化channelを`discount rate / demand / funding / common tail`から選ぶ。
3. 判断に必要なseriesと一次sourceだけ取得する。
4. series range、単位、公表日、取得日を確認し、結論を反証する系列も読む。
5. workflowの固定順に沿ってRegime summaryから監視ポイントまで8セクションを作る。各セクションでseries、fact、judgment、投資接続を分け、セクション7にbase / bear / bull・バーゲン地形・sector tilt・research優先度ヒント・sizing caution、セクション8に見方を変える条件を置く。decision-grade refreshではworkflowの深度契約（日本需要fact、円両側リスク、market-snapshot、日本株バリュエーションアンカー、hint識別力）を全項目満たす。
6. 個別thesisのどのscenario/claimを変えるかを1〜3行で示す。
7. publish前に敵対的self-checkを通す: (a) 各judgmentが引用factの数値と整合するか（数値⇄結論を突合）、(b) 結論を反証する系列を実際に読んだか、(c) 為替・金利の判断が両側リスクを持つか、(d) 各hintが候補タイプを判別できる識別力を持つか、(e) 各factの公表日が当該統計の最新公表か。fail項目は修正してから進む。
8. materialならstrict contractを満たすdraftを作り、確認したheadを`--expected-head`へ渡して`baibai-engine macro context publish`する。初回publishだけはexpected headを省略する。

## 禁止

market timing、cash比率、candidate hard gate、sector自動tilt、統計的edge、個別sizingを出さない。HTMLを必須成果物にしない。screeningの機械rankingをmacroで変更しない。
