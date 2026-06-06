---
title: "J-Quants rate limit observations"
summary: "J-Quants Light の非公開レート制限と bootstrap コストの、実運用からの観測・推測メモ。"
doc_type: reference
status: active
last_reviewed: 2026-06-06
related_docs:
  - "./data-sources.md"
  - "../operations/screening-runbook.md"
---

# J-Quants rate limit observations

J-Quants Light プランの正確なレート制限は非公開のため、**実運用で観測した挙動からの推測**をここに蓄積する。確定仕様ではない。新しい観測が得られたら追記し、徐々に精度を上げる。コード側の対処は `src/baibai_loop/screening/providers/jquants.py` の `_RATE_LIMIT_BACKOFF_SECONDS` / `_RANGE_CHUNK_DAYS` を参照。

## 観測サマリ（2026-06-06）

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

- **coverage-aware skip の強化**: daily_bars は物理的に 2023〜2026 が既に cache 済みでも、`source_coverage` に当該 asof 窓の chunk が `ok` 記録されていないと re-fetch される。物理行の存在から coverage を導出できれば re-fetch を大幅に減らせる可能性。
- **asof 間 coverage の共有**: 重複する履歴窓を asof ごとに別 coverage key で持つのではなく、窓の union で管理して 2 週目以降の re-fetch を削る。
- **必要履歴窓の見直し**: universe / 流動性指標に本当に 3.3 年必要かを確認し、短くできれば chunk 数が減る。
- **off-peak 実行**: throttling が軽い時間帯に回す。ただし上記のとおり主因は volume なので効果は限定的。

## 追記ログ

- 2026-06-06: 初版。`#157` の歴史 replay 生成が完了しない調査から、per-asof 長期履歴 re-fetch（~4 分/31日chunk）が律速と特定。
