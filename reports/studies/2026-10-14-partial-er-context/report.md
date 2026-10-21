---
title: "結果欠測から独立したER帯とbasis別の記述分布"
summary: "全宣言月を保持し、観測分布・未評価・formal evidenceを別々に届ける。"
doc_type: measurement-record
status: complete
date: 2026-10-14
---

# 結果欠測から独立したER帯とbasis別の記述分布

これはschema4のprivate previewを比較した時点の記録である。後続の生成物受入は
[v26固定19月の受入記録](../2026-10-14-er-context-v26/report.md)を参照する。

refs [#1516](https://github.com/koumatsumoto/baibai-loop/issues/1516) / [#1481](https://github.com/koumatsumoto/baibai-loop/issues/1481)。意味の正本は[Estimate calibration](../../../docs/reference/estimate-calibration.md#er-level-distribution-context)。

旧生成器は配当込みreturnまで観測できた対象からER帯を作り、priceも同じ対象へ限定していた。schema 4は有限ER全対象から固定境界を求め、同じ境界で両basisを分類する。結果欠測は境界を変えない。正常な観測不能では宣言月・母数・既存理由を残し、空basisの分布はnullにする。panel欠落・入力やforwardの不整合・method不一致はpartialへ読み替えない。

## 保存済みsnapshotによる比較

2019-11〜2026-09の83cohortの保存済みpanel/forwardを通常evaluatorで読み、取得・全buildを行わずprivate previewへ出力した。固定19月×3y/5yのformal evidenceは36/38、全83月×2では58/166のままである。記述contextの生成成功はformal completeness・方法採用・利益優位を意味しない。

| scope / horizon | 有限ER宣言数 | 旧price観測数（totalとのmatched sample） | 新price観測数 | total観測数 |
|---|---:|---:|---:|---:|
| 固定19月 / 3y | 66,802 | 57,112 | 63,208 | 57,112 |
| 固定19月 / 5y | 66,802 | 50,877 | 62,170 | 50,877 |
| 全83月 / 3y | 297,256 | 141,968 | 160,254 | 141,968 |
| 全83月 / 5y | 297,256 | 61,562 | 75,273 | 61,562 |

固定19月の四境界は−3.6881%、−0.8871%、1.4241%、3.5630%で両horizonに共通となった。独立したread-only比較と全band/basisのn・median・q25・q10・trap率が保存精度で一致した。83月のsnapshotはintegrity正常・FK違反0、cache contractは変更していない。

## 消費と限界

Research入力と既存Security Analysis表示へ各basisの宣言/観測/未評価・理由、宣言月数と観測月数、formal completenessを渡す。観測済み分布を全母集団の実証済期待収益・個別予測・購入条件へ代用しない。未評価を損益0へ補完しない。basisごとに観測対象と比較中央値が異なるので、price-total差を配当効果として読まない。ER値・selection/order・購入要求率は変更しない。識別範囲は今回の実装へ追加していない。

この測定では本番contextを公開していない。当時保持していた[schema 2 artifact](../2026-10-14-er-context-v26/retained-schema2-context.yaml)は原bytesのままで、SHA-256は`28ac630de29b0011df1505b081e7428b63f11da0a53fd8d9f460b1c3d3dce670`。旧artifactのschema/hash差替え、application DB・個人取引データの更新・公開は行っていない。

回帰は結果欠測で帯不変、配当欠測でもprice保持、完全ケース、全欠測basis、単一欠測で全月を隠さないこと、破損拒否、Research/表示への不足伝達を対象とする。再現は同じ保存snapshot・全required-asofから通常`calibration-evaluate --context-out`をprivate pathへ実行し、fixtureの成功だけを実データ受入へ読み替えない。
