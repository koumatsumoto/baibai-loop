---
title: "Glossary"
summary: "Glossary of Baibai-Loop terms used across architecture, operations, and component docs."
doc_type: reference
status: active
last_reviewed: 2026-05-04
---

# Glossary

| term | meaning |
| --- | --- |
| brief | `records/01-brief/`。マクロ事実ブリーフ。fact layer |
| candidates | `records/03-candidates/`。機械的 screening を通過した銘柄 snapshot。fact layer |
| outlook | `records/02-outlook/`。brief を source としたマクロ見解。analysis layer |
| research | `records/04-research/`。candidates と outlook を統合する個別銘柄 packet。analysis layer |
| trades | `records/05-trades/`。採用済み research に対する執行記録 |
| reviews | `records/06-reviews/`。trade 後 review と monthly retro |
| Macro gate | outlook の sector / region 判定を research 採用可否に接続する gate |
| playbook | `records/_playbooks/` に置く active rule |
| fact layer | 観測値、一次情報、機械的計算結果だけを扱う layer |
| analysis layer | fact layer を source として解釈、仮説、採用判定を扱う layer |
| shim | 旧 path 互換のため、移行通知と旧本文を残す docs |
