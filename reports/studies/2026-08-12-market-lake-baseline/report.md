---
title: "Market lake migration baseline"
summary: "巨大SQLite配布をimmutable partitionへ置換する前の容量、転送、runtime、pilot dataset coverage。"
doc_type: study
status: complete
as_of: "2026-08-12"
---

# Market lake migration baseline

## Purpose

`market.sqlite`全体をR2の配布単位にする現行経路と、最初のL1 pilotであるdaily bars /
short-sale reportsの規模を固定する。lake cutover後の比較は同じmetricを使い、changed partition
以外の転送が消えたか、screening runtimeと結果が維持されたかを判定する。

## Evidence boundary

- local `stores/market/market.sqlite`を`mode=ro&immutable=1`で読み、file size、
  `user_version`、PK row count、date coverageを取得した。
- R2転送とscreening runtimeは
  [cloud-daily-batch run 31585463272](https://github.com/koumatsumoto/baibai-loop/actions/runs/31585463272)
  のlogを使う。runはcommit `112ba1531c9df8710f876d50dbf4865fb6cd3772`で成功した。
- pull scriptはGET bytesを出力しない。GETはfull `market.sqlite` objectを取得するcontractであり、
  exact bytesは直前remote object versionのmetadataを記録しない限りrun logだけから復元できない。
  PUT bytes、snapshot / backup / upload時間はlogの直接観測値である。
- provider通信、R2 object、canonical storeへのwriteはこのstudyで行っていない。

## Store baseline

| metric | value |
| --- | ---: |
| `market.sqlite` bytes | 2,013,155,328 |
| decimal GB / binary GiB | 2.013 GB / 1.875 GiB |
| SQLite `user_version` | 23 |
| local file mtime | 2026-08-12 19:41:48 JST |
| `runs.sqlite` bytes | 52,801,536 |
| `macro.sqlite` bytes | 256,184,320 |

## Pilot dataset baseline

| dataset | rows | distinct tickers | coverage | additional |
| --- | ---: | ---: | --- | --- |
| `jquants_daily_bars` | 10,136,873 | 5,366 | 2016-08-01..2026-08-12 | PK `(ticker, traded_at)` |
| `jquants_short_sale_reports` | 1,412,135 | 4,112 | 2016-08-10..2026-08-12 | PK `(disclosed_at, source_ordinal)`、cancellation 6 rows |

row countとdate coverageはfact inventoryであり、全ticker・全営業日のcomplete point-in-time coverageを
主張しない。short-saleはcomplete daily snapshot、取消、無報告0、coverage不足を別のsemanticとして
parity比較する必要がある。

## Daily batch I/O and time

| metric | value | evidence kind |
| --- | ---: | --- |
| Pull stores elapsed | 33 s | job step `10:00:51..10:01:24` |
| Market GET bytes | full previous object; exact value not emitted | transfer contract + observability gap |
| Market PUT bytes | 2,013,155,328 | `store push` log |
| Market snapshot | 15 s | `store push` log |
| Market server-side backup | 190 s | `store push` log |
| Market upload | 181 s | `store push` log |
| All machine PUT bytes | 2,322,141,184 | logged market + runs + macro bytes |
| Upload machine stores and serving views | 455 s | job step `10:35:18..10:42:53` |
| Whole daily job | 2,567 s | job `10:00:19..10:43:06` |

直前にlogで確認できるsuccessful market uploadは
[run 31383046105](https://github.com/koumatsumoto/baibai-loop/actions/runs/31383046105)の
1,590,861,824 bytesである。ただしout-of-band publishの有無をrun logだけでは証明できないため、
これを次runのexact GET bytesとは断定しない。post-cutover比較ではGET / PUT request countとbytesを
publisher自身が記録する。

## Screening baseline

| metric | value |
| --- | ---: |
| `screening run` elapsed | 103.8 s |
| daily bars loaded | 3,368,820 rows |
| daily bars input range | 2023-04-30..2026-08-12 |
| financial summaries loaded | 36,045 rows |
| universe / candidates | 3,706 / 3,706 |
| run outcome | partial warning, published |

結果parityはrow PK set、normalized value、coverage、universe、candidate metric、reject reason、
selection / rankingを同じfrozen inputで比較する。elapsedだけの改善をcutover成功とは扱わない。

## Comparison contract

cutover後は次を同じ単位で再計測する。

1. routine updateのR2 GET / PUT request countとbytes
2. changed / reused partition数とdataset total bytes
3. build、validation、pointer switch、projection build、screeningのelapsed
4. pilot datasetのPK / value / coverage parity
5. frozen screeningのcandidate / metric / reject reason / selection parity

期待する差は、routine market transferがfull historyの2,013,155,328 bytesではなくchanged
partition sizeに比例することである。screening contractや判断結果の変更は期待差に含めない。
