# Cycle 2 operation log

価値tier: T2 — 円と JGB の機械監視面を復旧し、cycle-2 契約 6 項目を実運転して、Macro World Model の
採否判定に必要な 2 周目の証拠を作る。

## Identity

- `as_of`: 2026-08-12（市場データの最終完全営業日。8/11 は山の日で非営業日、8/12 は 4,437 銘柄の bar が揃う）
- context: `macro-context-2026-08-12-yen-retracement-real-rate-squeeze`
- predecessor: `macro-context-2026-08-07-labor-capex-divergence`
- evidence snapshot: `942d92637ec32bdff87d891e4996aa69ebaec35c706c3d694b9c6b11d1579adf`
- blind freeze: `e33acb7e4208139044795376a0f2c3a4997ba30c73adaa6deabb58eeadcc1bb7`
- scorecard snapshot: `scorecard-macro-context-2026-08-07-labor-capex-divergence-2026-08-12-f5eb31ec6282`
  （result digest `f3b5a145795179b42fb13b43008b053aec42aa6da7be0aba140b8162940013e9`）
- cloud workflow: `cloud-materialize` run `31601341210`（success。下記「クラウド反映」を参照）

## Execution

1. 19:59 JST に `batch/scripts/r2_transfer.sh pull-machine` を完了する。application DB は local 正本なので
   pull しない。日次バッチは同日 19:43 に success で終了しており、pull 中の競合は無い。
2. prior-blind preflight は head の `as_of=2026-08-07` と trigger `fired_count=0` だけを読み、head が判断時点に
   対して古いことと人間の明示 trigger を理由に `go` とする。head 本文と monitoring 内容は開かない。
3. `macro reading --asof 2026-08-12 --format json` で 122 系列を読む。stale 0 件、insufficient_history 0 件で
   data health の異常は無い。|z| ≥ 3 は 5 件（jp.policy_rate 3.69 / jp.2y 3.39 / jp.5y 3.30 / jp.10y 3.26 /
   jp.nikkei_pbr 3.11）、flags は us.erp の `non_positive` 1 件。
4. 機械 materiality の候補は 90 件。standing coverage 27・analyst addition 9 を足して候補 103、除外 6、
   選択 97 とする。除外はいずれも「既に選択した系列が同じ面を識別する」形の理由を持つ。
5. `build_evidence_snapshot` は 9,473 行の snapshot を出す。cycle 1 の 138,681 行から 93% の縮小であり、
   errata が記録した revision 窓の fix-forward が実データで効いていることを確認した。保存窓内で revision を
   持つ系列は 20 件（うち派生再計算 `derived_recompute: true` が 10 件、外部 source 由来が 10 件）で、
   materiality 理由に `recent_revision` が立つ系列は 19 件である。**この 2 つの集合は一致しない**
   （例: `us.core_capex_orders` は materiality が立つが保存窓には revision を持たない）。保存窓が
   materiality 判定を変えないという builder の契約が、実データで機能していることの確認になる。
   外部 source 由来 10 件のうち 4 件（copper・gold・silver・europe.stoxx）は同一営業日に複数 vintage を持つ
   市場価格の再配信であり、統計の改定は 6 件（jp.bank_lending_yoy・jp.real_consumption・
   us.average_hourly_earnings・us.claims_4wk_ma・us.initial_claims・us.nonfarm_payrolls）である。
6. charter・evidence packs・states 8 件・競合仮説 3 件・共通行列 28 行・4 horizon baseline・
   3 mechanism scenario・20 nodes / 24 edges の graph を作る。validator は evidence 138、states 8、
   hypotheses 3、scenarios 3 で `ok` を返す。
7. 20:42 JST に blind freeze を作る。freeze 対象 7 ファイルは prior context ID と prior 由来 key を含まない。
8. freeze 後に predecessor 本文を初めて開く。scorecard は `macro refresh` を要さず成立し、結果は
   `met 1 / not_met 0 / pending 5`。met は base の SOX 11,000 以上（11,993.86、8/10）。
9. revision diff を書き、`dropped_or_demoted` に 4 件を記録する。energy を支配的な力から第 3 位の競合仮説へ
   格下げ、通商措置を world model から落とし、`japan-breadth-test` の枠組みを 2 経路の非対称へ書き直し、
   前回 connection の座標「円157.8」を引き継がないことを理由付きで残した。
10. v4 を base 0.50 / bear 0.30 / bull 0.20 へ投影する。world model の adverse scenario
    `yen-slide-and-margin-squeeze` を bear へ直接写像したため inverse mapping を使わず、cycle 1 の
    projection note が記録した adverse tail の情報損失は発生していない。
11. `publish --check` は初回に 12 件の schema 違反を返した（material delta の channel / direction の
    literal、scenario direction の literal、economic_implications の型、sector tilt の direction、
    bargain_topography の shape）。修正後 `ok`。
12. self-check (a)–(p) で (i) が fail。risk_environment の judgment と stance summary に「構成は避ける」
    「感応度で選別する」という行動寄りの語があったため、環境評価の記述へ書き直して pass にした。
13. 21:28 JST に直前 head を確認し、predecessor を `--expected-head` に指定して実 publish する。直後の
    local head は新 context ID と一致する。

## 契約 6 項目の運転結果

| 契約 | 結果 |
| --- | --- |
| usd_jpy の機械条件を常置 | monitoring に `usd_jpy at_or_above 163.9` と `at_or_below 150.0` を置いた。前回 head は usd_jpy を 1 件も持たない |
| jp.10y の機械条件を常置 | `jp.10y at_or_above 3.0` と `at_or_below 2.5` を置いた。前回 head は jp.10y を持たない |
| `dropped_or_demoted` を含む両方向 diff | 4 件を記録。demote 2 件・drop 2 件 |
| adverse 保存則 | adverse scenario を bear へ直接写像し、inverse mapping による情報損失を回避した |
| relation_kind の基準 | `identified` は定義上の恒等式 3 edge のみ。DCF・行動的伝達は model_based / empirical / observational / judgmental へ割り当てた |
| economy-level の node 命名 | 20 node すべてが economy-level。portfolio・保有・配分・cashflow の語彙を使っていない |

機械監視面が実際に効くのは cloud の read model へ反映されてからである。`macro context publish` は
ローカル正本を進めるだけで、cloud 側の日次 trigger は反映まで前 head の条件で回る。本 cycle は
`push-app` と `cloud-materialize` まで実行しているため、上表の 2 行は cloud 側でも成立している。

## Point-in-time note

- data cutoff は 2026-08-12T20:00+09:00 とした。`as_of` は JST の市場営業日だが、cutoff は執筆時刻である。
  米 7 月消費者物価は米東部時間 8/12 の公表であり cutoff 後にあたるため、証拠に用いず forthcoming event
  として扱った。
- 歴史 replay は使わない（preregistration の固定条件）。機械読み値の point-in-time 品質については、
  独 10 年が月次で観測 2026-06、原油が 2026-08-03、日経平均（8/10）と PER・PBR（8/12）の観測が 2 営業日
  ずれることを state の uncertainty に明記した。
- USD/JPY は ECB 参照レート（中央欧州時間 16 時）であり、東京市場の終値ではない。

## Tier 1 の取得失敗

数値の代替埋めは行わない。BLS（Employment Situation・CPI release schedule）、METI（鉱工業生産）、
JPX（投資部門別売買）、東京商工リサーチ、Federal Register の官報 HTML は HTTP 403 または redirect で
取得できなかった。雇用者数と改定は L1 store の vintage から再計算し（5 月 158,861 / 6 月 158,881 /
7 月 158,858 千人 → 6 月 +20 千人・7 月 -23 千人）、通商措置は Federal Register の API から署名日
2026-08-06・公示日 2026-08-11・文書番号 2026-16400 だけを確認して税率と発効日は書かなかった。

## クラウド反映

`batch/scripts/publish.sh` は application DB（1,703,936 bytes）を R2 へ上げ、`cloud-materialize`
run `31601341210` を dispatch する。run は store pull、read model の materialize、serving views 3,738 と
serving tail 4 の upload をすべて成功させる。

反映は独立に検証できる。materialize が export したファイル数は、本 push の直前 run（`31598846613`）の
3,741 に対し 3,742 で、ちょうど 1 件増えている。増分は新 context の詳細 view であり、円と JGB の
machine_conditions は cloud 側の read model でも読める状態になった。

`push-app` は application DB を無条件に上書きする（`push_keys baibai.sqlite`）。これが安全なのは
application DB の正本がローカル側にあるためで、`pull_app` 自身がローカルにファイルがあれば cloud copy に
よる置換を拒否する。上書き前に世代バックアップを 1 つ残す。market / macro / runs の pull は行っていない。
これらは `push-app` の対象外であり、materialize は R2 側の store を読むため、ローカル側を置換する必要が
無いからである。
