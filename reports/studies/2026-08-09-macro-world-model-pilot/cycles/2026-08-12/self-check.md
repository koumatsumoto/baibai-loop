# Cycle 2 hard-fail self-check

この確認は Stage A promotion 判定ではなく、preregistered hard fail と `macro-context` skill の敵対的
self-check (a)–(p) に対する cycle artifact の自己検証である。独立 reviewer と owner blind preference は
実施しない。

## Preregistration hard fails

| # | result | evidence |
| ---: | --- | --- |
| 1–2 | pass | 4 key judgments はそれぞれ state / horizon / subgraph（node と edge）/ counter hypothesis / current-cycle evidence を持つ。単一 indicator の言い換えは無く、最少でも 5 evidence を束ねている。 |
| 3 | pass | 24 edges すべてが relation kind / claim strength / sign / lag 範囲 / current-cycle evidence / 名指しの falsifier を持つ（validator が強制）。 |
| 4 | pass | `identified` は 3 edge だけで、いずれも定義上の恒等式である（e04 株式リスクプレミアム = 益回り − 10 年国債利回り、e07 実質賃金 = 名目賃金 ÷ 物価、e12 外貨建て利益の円換算 = 外貨額 × 為替）。DCF・行動的伝達には `model_based_relation` / `externally_identified_empirical_relation` / `internal_observational_association` / `judgmental_hypothesis` を割り当てた。 |
| 5 | pass | 対立する証拠（日経 +60.1% と株式リスクプレミアム 5.1 パーセンタイル、IV 93.2 とスキュー 3.4、新規失業保険 2.3 と雇用者数 14.3、CCC 96.4 と HY 8.9、実質賃金 95.7 と消費者態度 27.1、介入額の未開示）は 6 件すべて `unresolved_tensions` に束縛した。 |
| 6 | pass（限界つき） | 判定条件は「shock / persistence / propagation / reaction の**いずれでも** baseline と異ならない」であり、1 次元でも差があれば該当しない。rank 2 と rank 3 は initial shock（介入効果の完全減衰 / ホルムズ制約の早期解消）から異なる。rank 1 の `capex-led-plateau` は shock が「新しい外生ショックは入らず、政策の現状維持が続く」で無ショック、persistence（実質割引率は上端に留まる）と policy reaction（BOJ 1 回の利上げ検討 / FOMC 据え置き）と paths（円 155-165、日 10 年 2.7-3.1%）が baseline_path の now / 0_3m / 3_12m と同内容だが、`propagation_delta` は「米国の設備投資が需要の穴を埋め、実質賃金が消費を下支えする。倒産の増加は中小に限られ、上場企業の需要には波及しない」と baseline に無い伝播の主張を持つ。したがって該当はしない。ただし rank 1 が 4 次元のうち 3 次元で baseline と区別できないことは記録に残す。cycle 1 の rank 1 は固有の initial shock を持っており、この点では後退している。 |
| 7 | pass | data cutoff は 2026-08-12T20:00+09:00。米 7 月消費者物価は cutoff 後の公表なので証拠に用いず、charter の conditioning assumption に明記した。全 external source の公表日は cutoff 以前である。 |
| 8 | pass | 歴史 replay は使わない（preregistration の固定条件どおり）。機械読み値の point-in-time 品質は、`ProviderSpec.point_in_time_vintage` を宣言する source だけが clamp される契約であることを踏まえ、独 10 年が月次で観測 2026-06、原油が 2026-08-03、日経平均と PER の観測が 2 営業日ずれることを state の uncertainty と operation log に明記した。 |
| 9 | pass | 3 scenario すべてに policy reaction を書いた。 |
| 10 | pass | 実質と名目（実質賃金と名目賃金、実質金利と名目金利）、水準と変化率（state の level と momentum を分離）、フローとストック（海外投資家フローは週次ネット -4,903 億円として扱い残高にしない）、期待と観測（政策経路ギャップは織り込みであり実現ではないと明記）を分けた。 |
| 11 | pass | 除外は 6 件で、いずれも「既に選択した系列が同じ面を識別する」形の具体的な理由を持つ。 |
| 12 | pass | 各 state・各 hypothesis が `evidence_against` を持ち、共通行列 28 行が 3 仮説すべてに supports / contradicts / mixed / neutral を割り当てる。全仮説に contradicts が付く行が存在する。 |
| 13 | pass | blind validator が prior context id と prior 由来 field を拒否し、freeze `e33acb7e4208139044795376a0f2c3a4997ba30c73adaa6deabb58eeadcc1bb7` が成立した。 |
| 14 | pass | freeze 後に開いた前回 head からの変更は `revision-diff.yaml` に隔離し、`dropped_or_demoted` 4 件に帰属させた。 |
| 15 | pass | one-page は renderer が world model の必須 5 部から生成し、coverage のためだけの段落を持たない。 |
| 16 | pass | 全 edge に series または公表 event の falsifier があり、world model の `signposts` 10 件のうち 2 件は日付が確定した公表イベント（2026-08-17 GDP、2026-09-17〜18 日銀会合）、2 件は日付未定の公表イベント（EIA 次回 STEO、財務省の外国為替平衡操作 月次公表）、残り 6 件は series の閾値である。 |

hard fail: **0**。ただし #6 は「いずれでも異ならない」という文言に救われた pass であり、rank 1 scenario は
4 次元のうち 3 次元で baseline と区別できない。実質的な代替経路は 3 本ではなく 2 本と読むのが正確で、
昇格判定では hard fail の件数ではなくこの情報量で評価する必要がある。base scenario を baseline から機構として
分離するか、#6 の判定対象を baseline 以外の scenario に限るかは preregistration の改訂を要する論点であり、
結果後に基準を動かさないため本 cycle では触れない。

## macro-context skill の敵対的 self-check

| 項目 | result | 補足 |
| --- | --- | --- |
| (a) judgment と引用 fact の数値整合 | pass | 各セクションの judgment を引用 percentile と水準で逐一照合した。 |
| (b) 機械的事実との矛盾 | pass | VIX 37.4 パーセンタイルの平静と MOVE の上昇・CCC の拡大の食い違いは、消さずに tension として書いた。 |
| (c) counter_evidence の実在性 | pass | 4 force すべてが実在の series と percentile、または一次 source を counter に置く。 |
| (d) 為替・金利の両側リスク | pass | fx の judgment が円安継続と円反転の非対称を 1 つの判断として書き、monitoring は usd_jpy と jp.10y に上下 2 条件ずつを置く。 |
| (e) research ヒントの識別力 | pass | 4 件の `applies_to` はいずれも定量条件または取引先属性で候補タイプを判別できる。 |
| (f) 各 fact が当該統計の最新公表か | pass | SLOOS は 7 月調査（2026-08-03 公表）、Beige Book は 7 月、EIA STEO は 8 月、FOMC は 7/29、ECB は 7/23、日銀は 7/31、倒産は 7 月分、雇用は 7 月分。米 7 月 CPI だけが cutoff 後で、その旨を明記した。 |
| (g) scorecard の機械照合性と期限 | pass | 6 条件すべてが series + 比較 + 閾値 + 期限を持ち、期限は 2026-12-31 と 2027-01-31。重複条件は無い（usd_jpy は比較演算が逆）。publish gate が検証済み。 |
| (h) machine_conditions の有無と引用 | pass | 7 監視点に 9 条件。usd_jpy と jp.10y は上下 2 条件ずつを常置し、全条件の series を monitoring が引用する。 |
| (i) core・synthesis の行動指示 | fixed | risk_environment の judgment と stance summary に「構成は避ける」「感応度で選別する」という行動寄りの語があったため、環境評価の記述へ書き直した。 |
| (j) force のチャネル波及 | pass | 4 force がそれぞれ 3 チャネルを名指しし、各チャネルへ相異なる系列を割り当てている。 |
| (k) 確率の整合と合計 | pass | 0.50 / 0.30 / 0.20 = 1.00。world model の plausibility 順と一致し、stance neutral と base 0.50 が整合する。 |
| (l) estimate_caveats の regime 特異性 | pass | 4 件は株式リスクプレミアム 5.1 パーセンタイル、低金利期を含む倍率平均、介入後の円の戻り、2 か月連続 1,000 件超の倒産という現局面の観測に接地する。 |
| (m) bargain_topography の接地 | pass | 20 日 +0.62%、60 日 +9.73%、breadth 64.1%、regime neutral_range、33 業種の上下位を market snapshot input から引用した。 |
| (n) 焦点 fact 規律 | pass | 全セクションで焦点 fact を先頭に置き、座標の網羅転記は末尾 1 件に隔離した。 |
| (o) 自前出力の枠 | pass | market snapshot と scorecard は `machine_snapshots`、reading は `reading_snapshots` に置いた。 |
| (p) 深度契約の逐一照合 | pass | 8 象限すべてに fact、外部記事は取得成功 17 本（Tier 1 中心）、日本需要は実質賃金と鉱工業生産、通商政策は Proclamation 11052 の一次確認、地政学 tail は FOMC・ECB・EIA、日本株アンカーは益回り − JGB 10 年。 |

## 取得できなかった Tier 1

数値の代替埋めは行わない。次の 3 件は `status: failed` として記録するか、事実の粒度を落として扱った。

- U.S. Bureau of Labor Statistics の Employment Situation と CPI release schedule は HTTP 403。雇用者数と改定は L1 store の vintage から再計算し（5 月 158,861 / 6 月 158,881 / 7 月 158,858 千人 → 6 月 +20 千人・7 月 -23 千人）、公表時刻は断定しなかった。
- Federal Register の官報全文は redirect で取得不能。API から署名日・公示日・文書番号だけを確認し、税率と発効日は書かなかった。
- 東京商工リサーチ・METI 鉱工業生産・JPX 投資部門別売買は取得不能。いずれも L1 store が同じ統計を保持しているため、数値は store の観測を使い、外部 source としては引用しなかった。
