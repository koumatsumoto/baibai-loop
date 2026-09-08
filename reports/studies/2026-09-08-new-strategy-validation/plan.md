# 新戦略実判断点検・事前固定

価値tier: T1 — 実判断の誤った見送りと重要仮定を点検し、次に改善する工程を特定する。

## 範囲と順序

[Issue #1263](https://github.com/koumatsumoto/baibai-loop/issues/1263)のS0→S1→S2→S3を実行する。#1249の母集団保存のみ並行し、Y/N/U判定はS1終了後に開始する。主担当は入力構造の確認中に原valuation値・unresolved理由を閲覧した。これは診断対象の原判断であり、新しいレビュー結果・将来returnは未閲覧。独立担当には原valuation、disposition、CAA、Triageの順位・理由を渡さず、一次資料と原価格から先に再構成させる。完全なLLMの過去知識遮断は主張しない。

実行基点main `317ca38c413c89872c36f8d7229eabd48a7f9051`。fetch後origin/mainと一致、開始時worktree clean。最新store取得状況はlocal copyの観測であり、cloud最新を保証しない。今回pull・production再計算は行わず、取得済み正本をSQLite `mode=ro`で読む。変動中の再現が必要ならbackup APIで一時隔離コピーを作り、WALを無視したcopyをしない。

## S0のexact入力

| ticker | 原Thesis ID | 原Review ID | Thesis公開時刻 | 原価格 円/株 | 原horizon 月 |
| --- | --- | --- | --- | --- | --- |
| 6675 | `thesis-6675-20260908-1244` | `thesis-review-20260908-6675-independent-assets` | 2026-09-08T08:53:43.739767+09:00 | 2177.0 | None |
| 5946 | `thesis-5946-20260908-1244` | `thesis-review-20260908-5946-independent-assets` | 2026-09-08T08:53:44.139367+09:00 | 2124.0 | None |
| 6430 | `thesis-6430-20260908-1244` | `review-6430-20260908-independent-earnings` | 2026-09-08T08:53:54.612298+09:00 | 2469.0 | 12 |
| 6199 | `thesis-6199-20260908-1244` | `review-6199-20260908-independent-earnings` | 2026-09-08T08:53:54.993853+09:00 | 1320.0 | 12 |

全4社の原価格観測は2026-09-07 15:30 JSTの未調整終値。原評価日は2026-09-08であり、Triage as-ofと区別する。null horizonは原未評価を保持し、12か月へ補完しない。CAAは`capital-allocation-assessment-20260908-four-cases`（2026-09-08T08:55:29.719249+09:00）、exact revisionをlatestへ置換しない。

取得時最新Triage `research-triage-20260907-5a886e2984a8b4f7`、Review Set `review-set-20260907-a14dfd52d1d9`、run `run-revision-7ab9668455a845cb878db12a5f7bdbce`。as-of 2026-09-07、run_at 2026-09-08T08:26:05.287906+09:00、Security Analysis母数はrunの3,705（完全性は#1249で照合）。#1249の情報cutoffは2026-09-07 JST日末で、S1は各原判断時刻までに公開された資料に限定する。

Candidate Discovery identity: `{"method_hash": "3e4865e072f6559c5873072e65f5b418722b5d096988bbb7caa7611c10b67145", "method_id": "multi-valuation-v4", "nomination_depth": 20}`。screening rules hash `c525a6c55309450f`。triage contract `research-triage-v3`。

| store | schema | local mtime UTC |
| --- | --- | --- |
| application/baibai.sqlite | 20 | 2026-09-08T00:30:32 |
| screening/runs.sqlite | 5 | 2026-09-07T23:28:48 |
| market/market.sqlite | 25 | 2026-09-07T23:45:26 |
| screening/calibration/current.sqlite | snapshot metaで確認する（user_version 0） | 2026-09-04T01:01:26 |

ledgerの確認済み範囲だけを読む。保有・予約・cashの実残高完全性は推定しない。active Operation `op-20260907-capital-allocation-1`を変更せず、S2は公開しないstudyメモで進める。既存較正はprice_return_only、method fidelity・成熟horizon・欠損を既存snapshotで点検し、rebuildしない。

## 結果前に固定する判定

S1は6675/6430/5946/6199全件を対象とし、欠損はunverifiable、他社へ置換しない。原source事実、NI×PER/EVの整合、負債・自己株・希薄化・分配二重計上、原価格に必要な利益/倍率、倍率回復なし、経済的遅延、unknownの重要性、訂正だけで変わる判断段階、再検討eventを調べる。判定はconfirmed_error / judgment_disagreement / supported_no_change / unverifiable。算術・当時事実の違反と将来仮定の異論を分ける。confirmed_errorはownerと受入条件を後続Issueへ切り出し、本番修正はこのPRに入れない。

現在の有効Reviewed Thesis・保有・予約・taskをread-only確認し、価格待ちとevent待ちを区別する。全保有の新規Thesisは作らない。該当なしはnot_applicable、入力不明はunverifiable。

#1249 rubricはY=具体的価値仮説と資本判断を変える調査質問、N=関連事実を十分確認してその経路を否定、U=重要資料欠損・時点矛盾・根拠ある不一致。安い指標、赤字、無配、Pmax外だけでラベルを決めない。S1でこの意味を点検後、標本判定前にrubric確認を固定する。原4社の結論一致は合格条件ではない。

S2はS1終了後に最新canonical Triageを再読し、research priority昇順、既保有/予約済みとThesis v4既存tickerを除く先頭最大4社を調査前に追記・固定する。0件は空集合で終了し、別日や条件で補充しない。一次情報→Base/Downside→独立Review→横比較→提案可否まで非canonicalメモにする。要求利回りや購入policyを下げない。

## 前向き観測と引継ぎ

原4社はprospectiveへ混ぜない。S2 decision後最初のJPX営業日closeを診断entryとし、原価格との見積り診断と分ける。未来はnot_observed。cash基準は金利0、ETF proxyは1306に固定。price-onlyと確定分配を含む税/費用控除前・分配再投資なしの診断を分け、分割等は原1株basisへ換算する。企業行動/分配不明を0にしない。公式TOPIXの任意期間が無ければ比較不能。portfolio-wide TWRは既存outcome ownerでのみ扱い、screening replay・draft診断と合算しない。

3m/6mと原評価日+原horizon満期（非営業日は以前直近営業日）を固定する。旧horizon延長で外れを消さない。12新ticker到達または初回decision+30暦日の早い方で負担/不能/誤りを点検する。将来成熟を今回の停止理由にしない。必要な観測のみ既存taskの重複を確認しcohort単位で登録。#973の3taskは確認のみ。登録不能ならpayloadと理由を保存する。

## 保存と検証

非公開原文・source・独立計算・draft・task payloadは元作業環境の`.cache/studies/new-strategy-validation/`。#1249の最小断面は`.cache/studies/opportunity-coverage/`。別環境ではこの非公開directoryの明示的引渡しが必要で、自動同期を仮定しない。欠落時はunverifiableとし原予測を再生成しない。Gitには集計・公開source・必要ID・結論だけを残す。

正式publish、ledger、Operation complete、R2 push、cloud dispatch、本番選定/売買条件の変更は禁止。AP-01/02/03/04/05/06/07/09/12を作業前・commit前・PR前に確認。full local gatesはpython-foundation §9、反証レビューはkm-reviewの実装者と独立product観点で行う。将来の勝率・収益優位や#1262単独の因果効果は認定しない。

## S1後のrubric確認・S2対象固定

固定時刻`2026-09-08T20:21:28.235889+09:00`。独立担当の原結論を隠した初回→原4社との照合を完了し、主が一次資料・株数・算術を突合した。原判断への新規confirmed_errorは0、6199の12倍などはjudgment_disagreement。Research Y/N/Uの定義は維持し、企業defer・購入価格外・将来利益の幅だけでN/Uへ落とさない。詳細はreportへ残す。この確認前には#1249の主標本判定を実施していない。

S2開始時に最新canonical Triageと全Thesis v4、報告済み資本eventの不変をread-onlyで再確認した。latestは上記exact Triageのまま。research priority先頭から既保有/予約とv4既存4社を除いた対象は次の4社で、補充・入替えはしない。

| ticker | priority | 2026-09-07 15:30 JST未調整終値 円 |
| --- | ---: | ---: |
| 6419 | 1 | 3,190 |
| 4231 | 2 | 1,051 |
| 2415 | 3 | 1,686 |
| 2221 | 4 | 3,065 |

4社ともThesis v3はあるがv4は無く、Issueの除外条件に該当しない。新戦略の新規caseであり、企業の初回調査とは呼ばない。原v3結論を新draftへ転記せず、同じ一次資料範囲から再構成する。情報cutoffは#1249との比較も揃えるため`2026-09-07T23:59:59.999999+09:00`、資料深度は最新短信・説明資料・年次財務/必要資本開示。これら4社のsource収集は判定無しで並行したが、主のS2結果調査は対象固定後に開始する。

private `s2-selection.json` SHA-256=`2aca7918f93a66a616a886c9cbd95af4e092d16958d540a7a79aac19a23fb785`。評価・decisionはこれから固定し、将来診断entryはdecisionより後の最初のJPX営業日close。現時点の原価格を将来entryへ代入しない。
