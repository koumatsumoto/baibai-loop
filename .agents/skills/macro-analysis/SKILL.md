---
name: macro-analysis
description: 金利・為替・流動性・需要・資金調達・共通tail riskのmaterial changeが個別企業の5年評価を変え得るとき、macro contextを確認・更新するために使う。
---

# Macro Analysis

## 正本

最初に[`docs/workflow/macro.md`](../../../docs/workflow/macro.md)を読む。series、provider、8分析レンズ、source tier、record schemaをskillへ再転記しない。

## Trigger

- discount rate、需要、資金調達、common tailにmaterial changeがある。
- 主要event後、または既存macro contextのrefresh triggerが発火した。
- 個別packetのscenario/claimを変える外部経路を確認する。

定期だからという理由だけでrecordを作らない。materialでなければ根拠を短く返して終了する。

## 手順

1. `baibai-engine macro context head`と`context show --latest --asof <date>`で既存contextの`as_of / valid_until / refresh_triggers`を確認する。
2. 変化channelを`discount rate / demand / funding / common tail`から選ぶ。
3. 判断に必要なseriesと一次sourceだけ取得する。
4. series range、単位、公表日、取得日を確認し、結論を反証する系列も読む。
5. 個別packetのどのscenario/claimを変えるかを1〜3行で示す。
6. materialならstrict contractを満たすdraftを作り、確認したheadを`--expected-head`へ渡して`baibai-engine macro context publish`する。初回publishだけはexpected headを省略する。

## 禁止

market timing、cash比率、candidate hard gate、sector自動tilt、統計的edge、個別sizingを出さない。HTMLを必須成果物にしない。screeningの機械rankingをmacroで変更しない。
