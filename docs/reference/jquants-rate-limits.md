---
title: "J-Quants rate limit observations"
summary: "J-Quants Light の非公開レート制限と bootstrap コストの、実運用からの観測・推測メモ。"
doc_type: reference
status: active
last_reviewed: 2026-06-06
related_docs:
  - "./data-sources.md"
  - "../workflow/screening.md"
---

# J-Quants rate limit observations

J-Quants Light プランの正確なレート制限は非公開のため、**実運用で観測した挙動からの推測**をここに蓄積する。確定仕様ではない。新しい観測が得られたら追記し、徐々に精度を上げる。コード側の対処は `src/baibai_loop/screening/providers/jquants.py` の `_RATE_LIMIT_BACKOFF_SECONDS` / `_RANGE_CHUNK_DAYS` を参照。

## 観測サマリ（2026-06-06）

> **更新（2026-06-06、`b9f4f6c` + coverage merge 後）**: 以下の daily_bars re-fetch コストは **修正前の挙動**。daily_bars の coverage を `source_coverage` ではなく行データから導出するようにし（DB が SSOT、[`./screening-runtime.md`](./screening-runtime.md) §11.1）、さらに range fetch の coverage 記録を overlapping / adjacent window の union merge に変更した。これにより既存 cache がある asof の bootstrap では daily_bars を再取得せず、財務サマリーも chunk 境界ずれで生じていたギャップを作らない。下記は経緯として残す。

`bootstrap-cache --asof <past>` が 10 分・45 分の timeout でも完了しなかった件の調査。`PYTHONUNBUFFERED=1` で進捗を timestamp 付きログに残し、`source_coverage` テーブルの `fetched_at_utc` を突き合わせた。

- **bootstrap は asof ごとに長期履歴を取り直す**。`bootstrap-cache --asof 2026-05-29` の daily_bars 取得範囲は `2023-02-14..2026-05-29`（約 3.3 年）。`get_eq_bars_daily_range` / `get_fin_summary_range` は `_RANGE_CHUNK_DAYS=31` で 31 日 chunk に分割され、各 chunk は ClientV2 内部で per-day API 呼び出しに fan-out する。3.3 年 ≒ daily_bars 約 40 chunk + fin_summary 約 40 chunk。
- **throttling 下のスループットは 31 日 chunk あたり約 4 分**（2026-06-06 夕方の観測。`2025-02-27..03-29` 完了 17:15:32 → `2025-03-30..04-29` 完了 17:19:42）。この調子だと 1 asof の完全 bootstrap は **daily_bars だけで ~2.7 時間、fin_summary を含めて数時間**規模になる。
- **これは「数分で回復する rate window」でも「日次クォータの枯渇」でもない**。コードのコメントは「rate window は数分」と書くが、観測では timeout を 45 分に延ばしても 1 週が終わらなかった。律速の主因は **per-asof の長期履歴 re-fetch のボリューム** であり、429 backoff（最大 600s）はそれに上乗せされる。
- **chunk は resumable**。`source_coverage` に chunk 単位で `status=ok` が記録され、中断しても完了済み chunk は再取得しない。複数 asof は履歴窓が大きく重複するため、**最初の 1 週の full bootstrap が高コストで、以降の週は非重複 chunk + その週の edinet だけで安価**になるはず。
- **`run` は cache-only**。coverage が揃えば provider を叩かず高速。歴史 replay の律速は `run` ではなく `bootstrap-cache` / `extract-edinet-metrics` の coverage 充足にある。

## 実運用上の含意

- 歴史週 replay（`#157`）の生成は、rate budget の回復を「待つ」問題ではなく、**長期履歴 coverage を一度埋め切る wall-clock** の問題。1 週ずつ長時間（各 1〜数時間）バックグラウンドで流し、resumable な性質を活かして複数セッションに跨いで充足させるのが現実的。
- 短い per-step timeout（10〜45 分）で kill すると、その週の coverage が未充足のままになり `run` が fail-fast する。kill せず完走させるか、完了済み chunk を活かして再開する。

## 改善アイデア（徐々に対処する）

確定の設計判断ではなく candidate。着手時は別 issue にする。

- **coverage-aware skip の強化** ✅ 対応済み（`b9f4f6c`）: daily_bars は行データから coverage を導出するようにした。物理行が窓の両端に達していれば `source_coverage` の bookkeeping に穴があっても re-fetch しない。
- **asof 間 coverage の共有** ✅ 対応済み（coverage merge）: range fetch（日次足 / 財務サマリー / 営業日カレンダ）の coverage 記録を、新規取得窓と overlapping / adjacent な `ok` window の union に merge するようにした。asof ごとに chunk 境界がずれても、一部だけ重なる re-fetch が既存窓を delete-and-shrink して残りを孤立させることがなくなり、財務サマリーの再 bootstrap で偽のギャップが出ない。
- **必要履歴窓の見直し**: universe / 流動性指標に本当に 3.3 年必要かを確認し、短くできれば chunk 数が減る。
- **off-peak 実行**: throttling が軽い時間帯に回す。ただし上記のとおり主因は volume なので効果は限定的。

## 追記ログ

- 2026-06-06: 初版。`#157` の歴史 replay 生成が完了しない調査から、per-asof 長期履歴 re-fetch（~4 分/31日chunk）が律速と特定。
- 2026-06-06: daily_bars coverage を行データ導出に変更（`b9f4f6c`）。既存 cache がある asof では daily_bars を再取得しない。
- 2026-06-06: range fetch の coverage 記録を overlapping / adjacent window の union merge に変更。財務サマリーの chunk 境界ずれで `source_coverage` にギャップが残り（行は揃っているのに `_range_covered` が gap 判定）bootstrap が fail-fast する事故を解消。
