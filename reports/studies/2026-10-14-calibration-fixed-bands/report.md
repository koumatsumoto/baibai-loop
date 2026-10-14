---
title: "較正帯の履歴構成員と候補lookupの一致"
summary: "固定19cohortで相対Qと固定境界の不一致を測り、schema 3で同じ境界による履歴集計へ統一する。"
doc_type: measurement-record
status: historical
---

# 較正帯の構成員一致（#1493）

価値tierはT1。schema 2生成器はcohortごとの相対五分位で履歴を集計し、境界の中央値を固定上限として保存していた。lookupは固定上限を使うため、履歴分布の構成員と現在の候補分類が一致しない。個別企業の要求率・E[r]算式・順位・境界の最適化は対象にしない。

実生成関数・typed horizon・lookupを通した合成反例では、Aの0..9、Bの100..109を各値10回（各100ticker）へ展開し0.01倍すると、固定上限は0.01/0.03/0.05/0.07となる。履歴Q1には1.00が含まれるがlookupはQ5とする。最低nを緩めて再現していない。

保存v6の同じ全19月末（2020-01-31〜2021-07-30）・同じpanel/forward・実績FY配当を備えた同じ`_cohort_entries`で、相対Qと実lookupのQを全件比較した。元store・scope・正式35/38の未成立判定は変更していない。原L1とmethod identityは[先行記録](../2026-10-14-screening-seasoning-review/report.md)を参照する。

| horizon | ticker/as-of観測 | 不一致 | 不一致率 |
| --- | ---: | ---: | ---: |
| 3y | 56,959 | 10,867 | 19.08% |
| 5y | 49,869 | 9,775 | 19.60% |

現生成器の保存済み境界を変えず、その境界へ同じ全観測を再分類したQ5の差は次のとおり。全Q1〜Q5は各19/19cohortでnonemptyだった。全帯・全cohortの比較は[boundary-comparison.json](./boundary-comparison.json)に残す。

| horizon | n 相対→固定 | FY配当込み年率median 相対→固定 | q10 相対→固定 | trap rate 相対→固定 |
| --- | --- | --- | --- | --- |
| 3y | 11,400→12,240 | 14.9034%→14.3522% | −0.6098%→−0.7189% | 11.0263%→12.5490% |
| 5y | 9,983→10,663 | 14.4753%→14.0712% | 0.9337%→0.6025% | 16.5982%→18.0718% |

trapはcohortの累積returnのpopulation medianから−20pt以下で、上記の年率分位と単位が異なる。月次窓は重複し、これら件数は独立会社数・標本数ではない。全38組を含む構成員診断であり、3組のintegrity失敗を除いた新contextや利益優位の実証ではない。

採用した修正は、四境界を保存精度の小数6桁で先に固定し、その同じ上限で履歴とlookupを分類してから集計する二段階方式である。中央値・分位・n・trap・cohort等重みも同じ構成員から作る。同値は同じ帯へ置き、境界重複・空帯・cohort coverage不足は拒否する。8.5%の独立帯は維持する。定義は[較正reference](../../../docs/reference/estimate-calibration.md#er-level-distribution-context)を参照する。

schema 3を固定境界集計の意味として採り、schema 2の互換読取は加えない。元published artifactのSHA-256 `28ac630de29b0011df1505b081e7428b63f11da0a53fd8d9f460b1c3d3dce670`は不変で、CIはその原bytesの保持だけを確認する。旧artifactはmatching identityでも現行readerでは不適用となる。

修正後の同じ全観測で構成員の範囲不一致は3y 0/56,959、5y 0/49,869となり、全Q1〜Q5の統計は独立に再分類した集計と一致した。[検証結果](./fixed-membership-validation.json)に残す。正式生成器へ元のblocked evidenceを渡すと、引き続き`evidence is not complete`で拒否した。直接の帯集計による診断結果を正式contextとして発行していない。

振舞い検証は丸め近傍・境界上の同値・全6bandの構成員/分布/lookup一致、分離cohortの空帯拒否、同値/丸めによる重複境界拒否、旧schema拒否、schema 3のWeb契約を対象とする。完全gateとCIは提出時のPRへ記録する。本番store・published artifactの書換え・新context採用は行っていない。H3退出資料の修正は別PRで扱う。
