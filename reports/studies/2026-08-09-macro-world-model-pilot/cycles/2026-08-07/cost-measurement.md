# Cycle 1 cost measurement

計測は shell timestamp と作業 checkpoint に基づく実 wall-clock で、1 分未満を四捨五入する。手戻りは原因となった pass に含める。token usage は実行環境から取得できないため記録しない。

| pass | 内容 | wall-clock |
| ---: | --- | ---: |
| 1 | charter | 1 min |
| 2 | data audit（pull / reading / market / primary releases） | 3 min |
| 3 | coverage scan と manifest | 3 min |
| 4 | block 別 evidence packs | 2 min |
| 5 | typed states | 2 min |
| 6 | competing hypotheses | 1 min |
| 7 | common evidence matrix | 1 min |
| 8 | horizon baseline と mechanism scenarios | 1 min |
| 9 | sparse graph | 1 min |
| 10 | coherence challenge と validator（YAML date scalar 修正を含む） | 2 min |
| 11 | revision diff、indicator scaffold、v4 導出、`--check` | 10 min |
| 12 | CAS publish | 1 min |

- authoring / publish 合計: **27 min、12 passes**（15:13–15:40 JST）
- v4 導出部分: **10 min**（pass 11、全体の 37%）
- push-app / second-run checks / cloud materialize / serving verification: **7 min**（authoring budget 外、15:40–15:47 JST）
- end-to-end operation: **34 min**
- token: unavailable

第 1 サイクルは preregistration の baseline 計測であり、wall-clock 上限は事後に置かない。pass 数は事前上限の 12 以内（12/12）。次サイクル予算は preregistration の式により、この baseline と比較して固定する。
