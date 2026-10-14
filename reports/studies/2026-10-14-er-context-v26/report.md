---
title: "v26固定19月のschema4 ER文脈の受入"
summary: "生成済みの全fieldを保持し、basis別の欠測、formal evidenceの未成立、過去EDINET軸の未観測を示す。"
doc_type: measurement-record
status: complete
date: 2026-10-14
---

# v26固定19月のschema4 ER文脈の受入

refs [#1481](https://github.com/koumatsumoto/baibai-loop/issues/1481) / [#1516](https://github.com/koumatsumoto/baibai-loop/issues/1516)。意味の正本は[Estimate calibration](../../../docs/reference/estimate-calibration.md#er-level-distribution-context)。[最新artifact](../../published/er-level-calibration-latest.yaml)には生成済みschema4 previewの全field・元bytesを反映した。生成日は2026-10-14 JST、有効期限は2026-11-28。PR段階であり、本番公開・serving反映はしていない。

## 生成とidentity

生成codeはmain `674ef876fdf0dce44431c642e8804c4c6a2db36f`、受入codeはDPS情報を追加したmain `a3ef90029a2c620603ff15d88044890621c79c89`。両方でcalculation revision `dividend-period-freshness-v26`、production hash `aa922907ebc421a0`、panel hash `52f76944347543e8`、cache contract `b8c4df47e8842793`、ER model `expected-return-v1`が一致する。

通常buildは一度のみ、2026-10-14 05:58:16〜06:16:13 UTC（17分56秒）、固定19月×3y/5yの通常evaluateは06:16:38 UTCに完了した。両方exit 0。今回のPRで再build・追加provider取得はしていない。19/19 cohortがforward-ready、panel 70,263行、forward 351,410行（通常builderの全5horizon）、integrity正常、FK違反0。評価scopeは`estimator_policy`、mandatory metricは`er_calibration`、追加required metricは`er_level_calibration`。

新contextのSHA-256は`89cc4917964d7b8e89e04e2af8acc0596a21662c88e8ee1a455ed22c3661bb89`。旧schema2は[履歴artifact](retained-schema2-context.yaml)へ元bytesを保存し、SHA-256 `28ac630de29b0011df1505b081e7428b63f11da0a53fd8d9f460b1c3d3dce670`を維持した。旧83月snapshotと入力marketも開始前後のSHA・bytes・mtimeが一致した。旧統計のschema/hash付替えは行っていない。

## 固定帯と観測対象

結果の観測可否で帯を選ばず、全19宣言月の有限ER対象66,805件から各cohortの五分位上限を求め、cohort中央値を小数6桁で固定した。四境界は`[-0.036881, -0.008871, 0.014241, 0.035630]`（−3.6881%、−0.8871%、1.4241%、3.5630%）。両horizon・両basisに同じ境界を使う。境界と同値は下側quintileへ入る。独立した`er_gte_8_5pct`帯は8.5%以上で、quintile集計とは重複する。

旧methodの固定19月previewも同じ数値の境界だった。新methodではpanelのER annual 540行等が変わり、有限ER対象は旧66,802から3件増えた。今回の境界は新panel/forwardから生成した結果であり、同じ境界だけを根拠にmethodを同一視しない。旧固定19月と新snapshotのrow追加/削除は0、forward 351,410行は全field一致した。panelのfield別差分とidentityは[受入証跡](acceptance.json)に保存した。#1519と#1524の個別因果寄与へは分解していない。

次の数はQ1〜Q5だけの合計で、8.5%帯を二重加算しない。各basisは宣言数66,805。旧previewとの比較では全basisで観測+2、未評価+1。

| horizon / basis | 観測 | 未評価 | entry欠測 | stale exit | 配当欠測 | 配当split basis不明 | FY観測なし |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 3y / fy_actual_dividend_total_return | 57,114 | 9,691 | 1 | 3,594 | 4,190 | 1,884 | 22 |
| 3y / price_return_only | 63,210 | 3,595 | 1 | 3,594 | 0 | 0 | 0 |
| 5y / fy_actual_dividend_total_return | 50,879 | 15,926 | 1 | 4,632 | 7,314 | 3,957 | 22 |
| 5y / price_return_only | 62,172 | 4,633 | 1 | 4,632 | 0 | 0 | 0 |

帯ごとの宣言/観測/未評価・既存status理由、予測median、ticker-as-of等重みとcohort等重みのmedian/q25/q10/trap率、宣言/観測月数、median nは最新artifactの全fieldを参照する。未評価を0損益へ補完せず、配当欠測時のtotalへpriceを代用しない。basisごとに観測対象・累積returnの比較中央値が異なり、price-total差を配当効果とは解釈しない。

## Formal evidenceと過去入力の限界

`evidence_complete: false`、正式scopeは36/38成立。2020-06-30の3y/5yが`integrity_blocked` / `entry_price_gap`で未成立のままである。対象月を除外せず、正常な観測不能を持つ全19月の記述分布として保存した。Research入力と既存Security Analysis表示へ渡すformal completenessはfalseで、blockerとbasis別不足も残る。新methodのreader受理、旧methodでの`rules_identity_mismatch`、Web sourceでfalseと不足理由が保持されることを検証した。

7531は2020-06-10の2,750円を最後に、6/11〜6/30の原典barがOHLC・Vo・Vaすべてnullである。[JPXの2020年6月相場表23頁](https://www.jpx.co.jp/markets/statistics-equities/price/nlsgeu000004ttjj-att/st_202006.pdf#page=23)も当月価格の観測日数3・売買高1,000株を示す。[J-Quants仕様](https://jpx-jquants.com/ja/spec/eq-bars-daily)の当日約定なしと整合し、無約定と終日停止の理由は区別できない。6/30の仮想購入可能性は未確認で、15日許容を緩めず古値・未来価格を代用していない。

全19月の既存diagnosticsは次の通り。`population_edinet_axis_nonnull=0`は未観測であり、EDINET情報を復元済みとは扱わない。過去改訂復元の限界 [#671](https://github.com/koumatsumoto/baibai-loop/issues/671) / [#760](https://github.com/koumatsumoto/baibai-loop/issues/760)は未解決である。

| asof | master_snapshot_status | asof_population_mismatch_count | population_edinet_axis_nonnull |
| --- | --- | ---: | ---: |
| 2020-01-31 | exact_date | 0 | 0 |
| 2020-02-28 | exact_date | 0 | 0 |
| 2020-03-31 | exact_date | 0 | 0 |
| 2020-04-30 | exact_date | 0 | 0 |
| 2020-05-29 | exact_date | 0 | 0 |
| 2020-06-30 | exact_date | 0 | 0 |
| 2020-07-31 | exact_date | 0 | 0 |
| 2020-08-31 | exact_date | 0 | 0 |
| 2020-09-30 | exact_date | 0 | 0 |
| 2020-10-30 | exact_date | 0 | 0 |
| 2020-11-30 | exact_date | 0 | 0 |
| 2020-12-30 | exact_date | 0 | 0 |
| 2021-01-29 | exact_date | 0 | 0 |
| 2021-02-26 | exact_date | 0 | 0 |
| 2021-03-31 | exact_date | 0 | 0 |
| 2021-04-30 | exact_date | 0 | 0 |
| 2021-05-31 | exact_date | 0 | 0 |
| 2021-06-30 | exact_date | 0 | 0 |
| 2021-07-30 | exact_date | 0 | 0 |

このcontextは観測済みの歴史分布であり、未評価を含む全母集団の実証済期待収益、4 Approach全手法の成果実証・fidelity、企業の利益優位、個別予測、購入条件を成立させない。月次窓は重複し、月数を独立標本数と呼ばない。

## 再現と公開境界

完全local gateはPython 4,322 passed / 8 skipped、frontend 299 passed、Worker 93 passed。ruff・mypy・import contract・全drift gate・bandit・Python/Node依存監査・frontend build・Worker型検査とdeploy dry-runが成功した。変更したreader/Web source testは34 passed。元previewのbyte一致、旧schema2の元bytes一致、全quintile/basis集計と最新artifactの一致も確認した。これらは契約と搬入整合の検証であり、利益優位やformal completenessの成立を意味しない。

受入証跡の`evaluation.scope.required_asofs`全19日、3y/5y、同じrules・snapshotを通常`calibration-evaluate --run-purpose empirical_change_evidence --decision-subject estimator_policy --required-metric er_level_calibration --context-out`へ渡す。今回PRでは既存生成物をbyte一致で搬入し、evaluateも再実行していない。生成日時を含む元SHAとreaderのidentity受理を照合する。新snapshotがない環境で旧83月cacheを付け替えて再現しない。

PRに載せるのは公開市場資料由来の集計artifact・診断と合成testのみ。個人の台帳・注文・holding・application DB・認証情報を読み取らず、差分へ含めない。draft PRとCI確認までが今回の操作範囲で、本番公開は親レビュー後の別操作。公開時には新method operative runのidentity照合が必要で、旧runをretagしない。
