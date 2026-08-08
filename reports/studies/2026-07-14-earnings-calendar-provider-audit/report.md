# 決算発表予定 provider 監査（2026-07-14）

## 結論

screening の `next_earnings_date` は、J-Quants ClientV2 `get_eq_earnings_cal` ではなく、JPX の[決算発表予定日時一覧](https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html)に現在掲載されている全 cohort Excel を合成した snapshot から取得する。

JPX は issuer の公表予定を一覧で提供し、現在の May / June cohort を同時に読むことで、片方だけでは欠ける今後の発表予定を保持できる。これは通知サービスではない。候補調査と保有 review の実施時には会社 IR で最新日程と実際の発表を再確認する。

## 旧経路の観測

- local cache と live J-Quants ClientV2 はともに 14 行を返し、全行の発表日が `2026-05-20` だった。
- [公式 Python client](https://github.com/J-Quants/jquants-api-client-python)の ClientV2 `get_eq_earnings_cal(self)` 自体に日付 range parameter がない。adapter は `asof` から 90 日先までを要求する契約を表現できない method を呼んでいた。
- store は要求した 90 日を実データの coverage として記録したため、実体が 14 行・単一日でも coverage が green になった。
- この状態では、`next_earnings_date: null` が「既知予定なし」ではなく provider の不完全取得でも発生し、screening の event fact として区別できない。

## JPX 実データ監査

2026-07-14 に公式 index が掲載していた 2 file を取得し、semantic header と ticker/date を機械 parse した。

| file | parseable ticker rows | valid dates | undecided | date range |
| --- | ---: | ---: | ---: | --- |
| `kessan05_0703.xlsx` (As of 2026-07-02) | 447 | 441 | 6 | 2026-06-19〜2026-07-22 |
| `kessan06_0710.xlsx` (As of 2026-07-09) | 2,918 | 2,856 | 62 | 2026-07-03〜2026-08-26 |
| 合成 | 3,365 | 3,297 | 68 | 2026-06-19〜2026-08-26 |

- cohort 間の ticker 重複は 0 件だった。
- `2026-07-14` 以後の既知予定は 3,075 件だった。
- 予備観測の `2,923 raw / 2,856 valid / 62 undecided / 29 business days / 2026-07-03〜2026-08-26 / asof 以後 2,854` は、最新の `kessan06_0710.xlsx` だけを footer 込みで数えた値だった。全 current cohort を合成する機械契約では、footer を raw record に数えず上表の値を使う。

## 比較した選択肢

| 選択肢 | 判定 | 理由 |
| --- | --- | --- |
| J-Quants ClientV2 を継続 | 不採用 | range が呼出しへ伝わらず、実取得と coverage が一致しない。live でも単一日の 14 行だった |
| JPX 最新 cohort だけ | 不採用 | 直前 cohort に残る asof 以後の予定を欠落させる |
| JPX index の全 current cohort | 採用 | 一次 source で、現在公表中の既知予定を cohort 横断で保持できる |
| issuer IR を全社巡回 | screening input には不採用 | 最終確認には最も強いが、全上場銘柄の deterministic snapshot としては取得コストと layout 差が大きい |

## 効果と複雑性

2026-07-08 candidates 1,610 ticker と 2026-07-14 以後の JPX 既知予定を照合すると、1,319 ticker (81.9%) に `next_earnings_date` を付与でき、291 ticker は `null` だった。`null` は未定または現在の cohort 外であり、旧経路のような実質全件欠損ではない。

実装は既存 JPX provider に公式 index + Excel parser を加え、既存互換 table を JPX 論理 source で読み書きする 1 系統に保つ。新しい notifier、永続 schema、migration は追加しない。raw / valid / undecided / rejected と source URL は取得時 log と本監査に残し、SQLite coverage は安定 key、実日付範囲、保存件数だけを権威にする。

## 契約と限界

- index と file は `www.jpx.co.jp` の financial-announcement path に限定し、全 `kessan*.xls[x]` を解決する。
- bilingual header を日本語 semantic label で検出し、4 文字英数字 ticker と日付を正規化する。`未定` は既知日程から除外する。
- layout drift、不正 ticker/date、同一 ticker の矛盾日、download error、valid 0 件、全件過去は失敗として可視化する。
- SQLite coverage は実保存行の件数・最小日・最大日と照合する。`--allow-stale-jpx` は 7 平日の freshness だけを緩和し、partial / zero / all-past を許可しない。
- JPX 公表後も issuer が日程を変更する可能性があり、snapshot は point-in-time の完全履歴ではない。`next_earnings_date` は公表済みの既知予定という補助事実で、通知・決算実施・holding review trigger を保証しない。
- 候補の一次調査、dated holding task、決算後 review では会社 IR を再確認する。
